"""
One-command ingestion: PDF -> OCR -> clean -> chunk -> search index.

Runs the existing stage scripts in order, stopping if any one fails.

The `score` stage is the one human-in-the-loop step: it grades every page's
OCR quality and needs a confidence THRESHOLD to split pages into keep/skip
buckets. Without a threshold it only prints the distribution (so you can pick
one), and the pipeline pauses there. Pass --threshold to score AND run through
to the search index in a single shot.

Usage:
    # First pass: OCR the PDF, then stop at score to inspect page quality.
    python -m src.ingestion.pipeline data/raw/bundy-part-02.pdf

    # Second pass: commit to a threshold and finish (re-runs score only,
    # skips the slow probe + OCR you already did).
    python -m src.ingestion.pipeline data/raw/bundy-part-02.pdf --from score --threshold 60

    # Or do it all at once if you already know the threshold you want.
    python -m src.ingestion.pipeline data/raw/bundy-part-02.pdf --threshold 60

    python -m src.ingestion.pipeline data/raw/bundy-part-02.pdf --dry-run
"""

import argparse
import subprocess
import sys
from pathlib import Path

# Windows consoles default to cp1252 and mangle the em-dash in our status
# lines. The stage scripts all do this too — keep the pipeline consistent.
sys.stdout.reconfigure(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parents[2]
ING = REPO_ROOT / "src" / "ingestion"
RET = REPO_ROOT / "src" / "retrieval"


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the full ingestion pipeline on one PDF.")
    ap.add_argument("pdf", type=Path, help="Path to the source PDF, e.g. data/raw/bundy-part-02.pdf")
    ap.add_argument("--from", dest="start", help="Resume from a stage (e.g. clean)")
    ap.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="OCR-confidence cut for the score stage. Omit it and the pipeline "
             "stops after score so you can eyeball the distribution first; pass "
             "it (e.g. --threshold 60) to score AND continue through embed.",
    )
    ap.add_argument("--dry-run", action="store_true", help="Print the steps without running them")
    args = ap.parse_args()

    # Every output path is derived from the PDF's filename ("stem").
    stem = args.pdf.stem                       # bundy-part-02
    ocr_dir = REPO_ROOT / "data" / "ocr" / stem
    pages = ocr_dir / "pages.jsonl"            # built by ocr, grown by score/clean/group
    chunks = ocr_dir / "chunks.jsonl"          # built by chunk, read by embed

    # score is special: run bare, it only REPORTS the page-quality distribution;
    # given a threshold, it writes the `bucket` field that clean/group/chunk all
    # depend on. So the threshold, when supplied, rides along on the score command.
    score_cmd = [ING / "score_pages.py", pages]
    if args.threshold is not None:
        score_cmd += ["--threshold", str(args.threshold)]

    # The relay: each stage is just (name, the exact command to run).
    stages = [
        ("probe",  [ING / "probe.py",           args.pdf]),
        ("ocr",    [ING / "ocr.py",             args.pdf]),
        ("score",  score_cmd),
        ("clean",  [ING / "clean_pages.py",     pages]),
        ("group",  [ING / "group_documents.py", pages]),
        ("chunk",  [ING / "chunk_documents.py", pages]),
        ("embed",  [RET / "embed_chunks.py",    chunks]),
    ]

    # --from clean  ->  skip everything before the "clean" stage.
    if args.start:
        names = [name for name, _ in stages]
        if args.start not in names:
            sys.exit(f"Unknown stage '{args.start}'. Choose from: {', '.join(names)}")
        stages = stages[names.index(args.start):]

    for i, (name, cmd) in enumerate(stages, 1):
        printable = " ".join(str(c) for c in cmd)
        print(f"[{i}/{len(stages)}] {name:6}  {printable}")

        # When score runs without a threshold it writes no buckets, so clean
        # would fail next. That's not an error — it's the intended pause point.
        is_score_pause = name == "score" and args.threshold is None

        if not args.dry_run:
            result = subprocess.run([sys.executable, *map(str, cmd)], cwd=REPO_ROOT)
            if result.returncode != 0:
                sys.exit(f"\n[FAIL] Stage '{name}' failed (exit {result.returncode}). Stopping here.")

        if is_score_pause:
            print("\n[PAUSE] score ran in report-only mode — no threshold set, no buckets written.")
            print("  Review the distribution above, then resume with a threshold")
            print("  (re-runs score only, skips the slow probe + OCR):")
            print(f"    python -m src.ingestion.pipeline {args.pdf} --from score --threshold 60")
            return

    print("\n[OK] Done." if not args.dry_run else "\n(dry run - nothing executed)")


if __name__ == "__main__":
    main()
