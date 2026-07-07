"""
One-command ingestion: PDF -> OCR -> clean -> chunk -> search index.

Runs the existing stage scripts in order, stopping if any one fails.
Usage:
    python -m src.ingestion.pipeline data/raw/bundy-part-02.pdf
    python -m src.ingestion.pipeline data/raw/bundy-part-02.pdf --from clean
    python -m src.ingestion.pipeline data/raw/bundy-part-02.pdf --dry-run
"""

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ING = REPO_ROOT / "src" / "ingestion"
RET = REPO_ROOT / "src" / "retrieval"


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the full ingestion pipeline on one PDF.")
    ap.add_argument("pdf", type=Path, help="Path to the source PDF, e.g. data/raw/bundy-part-02.pdf")
    ap.add_argument("--from", dest="start", help="Resume from a stage (e.g. clean)")
    ap.add_argument("--dry-run", action="store_true", help="Print the steps without running them")
    args = ap.parse_args()

    # Every output path is derived from the PDF's filename ("stem").
    stem = args.pdf.stem                       # bundy-part-02
    ocr_dir = REPO_ROOT / "data" / "ocr" / stem
    pages = ocr_dir / "pages.jsonl"            # built by ocr, grown by score/clean/group
    chunks = ocr_dir / "chunks.jsonl"          # built by chunk, read by embed

    # The relay: each stage is just (name, the exact command to run).
    stages = [
        ("probe",  [ING / "probe.py",           args.pdf]),
        ("ocr",    [ING / "ocr.py",             args.pdf]),
        ("score",  [ING / "score_pages.py",     pages]),
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
        if args.dry_run:
            continue
        result = subprocess.run([sys.executable, *map(str, cmd)], cwd=REPO_ROOT)
        if result.returncode != 0:
            sys.exit(f"\n[FAIL] Stage '{name}' failed (exit {result.returncode}). Stopping here.")

    print("\n[OK] Done." if not args.dry_run else "\n(dry run - nothing executed)")


if __name__ == "__main__":
    main()
