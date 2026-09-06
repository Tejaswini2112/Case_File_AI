"""
Ingest a folder of PDFs in one command.

The pipeline handles exactly one PDF per invocation, which is fine for three
documents and impossible for a hundred: you would run a hundred commands, and
the fourth failing would leave the remaining ninety-six untouched.

    python -m src.ingestion.batch data/cases/bundy/raw/scans/ --case bundy \n        --threshold 60 --workers 8
    python -m src.ingestion.batch data/cases/bundy/raw/scans/ --case bundy \n        --threshold 60 --dry-run

Each PDF runs through the existing pipeline as a subprocess. That boundary is
the point: a corrupt file that kills the interpreter, or a segfault inside
poppler, takes down one file rather than the batch. Failure isolation comes
from the process boundary rather than from exceptions someone had to predict.

Progress is one line per file. Per-page detail goes to
data/cases/<case>/ocr/<stem>/ingest.log, because fifty files of page-by-page output is
thousands of lines nobody reads.
"""

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from pdf2image import pdfinfo_from_path

from src import paths
from src.cases import validate as validate_case
from src.ingestion.ocr import find_poppler_bin, source_fingerprint
from src.ingestion.score_pages import EXIT_NEEDS_REVIEW

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parents[2]

# Measured on this corpus at 300 dpi: ~2.3 s per page single-threaded, and
# about 2.4x faster on 8 workers rather than 8x. Used only for the --dry-run
# estimate, so it needs to be roughly right rather than exact.
SECONDS_PER_PAGE = 2.3
PARALLEL_EFFICIENCY = 0.3


@dataclass
class Job:
    pdf: Path
    pages: int
    fingerprint: str


def discover(paths: list[Path]) -> list[Path]:
    """Expand folders into the PDFs inside them, leaving explicit files alone."""
    found: list[Path] = []
    for path in paths:
        if path.is_dir():
            found.extend(sorted(path.glob("*.pdf")))
        elif path.suffix.lower() == ".pdf":
            found.append(path)
        else:
            print(f"  skipping {path} (not a PDF or folder)")
    return found


def load_state(state_path: Path) -> dict:
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # A damaged state file must not stop a run. The cost of losing it is
        # redoing work that is itself idempotent, so treating it as empty is
        # safe; refusing to start would not be.
        print("  warning: batch-state.json unreadable, treating as empty")
        return {}


def save_state(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(state_path)


def is_complete(state: dict, job: Job, case: str) -> bool:
    """True when this exact PDF finished a previous run and its output survives.

    Three conditions rather than one. The recorded status could be stale, the
    fingerprint catches a file replaced since, and the chunks file catches
    output deleted by hand. Any doubt re-runs the file, which is cheap because
    OCR itself skips finished pages.
    """
    entry = state.get(job.pdf.name)
    if not entry or entry.get("status") != "ok":
        return False
    if entry.get("fingerprint") != job.fingerprint:
        return False
    return (paths.ocr_dir(case, job.pdf.stem) / "chunks.jsonl").exists()


def build_jobs(
    pdfs: list[Path], poppler_path: str | None
) -> tuple[list[Job], list[tuple[Path, str]]]:
    """Read page counts and fingerprints without rendering anything.

    Returns the readable files as jobs and the unreadable ones separately.
    Unreadable files are failures, not absences: dropping them silently would
    let a batch where half the folder is corrupt report a clean success and
    exit zero, which is exactly the outcome a scheduled run must not produce.
    Catching them here rather than at OCR time also means a bad file is named
    in the first second rather than forty minutes in.
    """
    jobs: list[Job] = []
    unreadable: list[tuple[Path, str]] = []
    for pdf in pdfs:
        try:
            pages = int(pdfinfo_from_path(str(pdf), poppler_path=poppler_path)["Pages"])
        except Exception as exc:
            # Poppler writes its own parse errors to stderr, which are noisy and
            # already visible; the first line of the exception is the useful part.
            reason = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            unreadable.append((pdf, reason))
            continue
        jobs.append(Job(pdf=pdf, pages=pages, fingerprint=source_fingerprint(pdf)))
    return jobs, unreadable


def estimate_seconds(pages: int, workers: int) -> float:
    """Rough wall-clock estimate for the dry run.

    Parallel speedup is nowhere near linear — 8 workers measured 2.4x, not 8x —
    so the efficiency factor is deliberately pessimistic. An estimate that runs
    under is more useful than one that runs over.
    """
    if workers <= 1:
        return pages * SECONDS_PER_PAGE
    return pages * SECONDS_PER_PAGE / (1 + (workers - 1) * PARALLEL_EFFICIENCY)


def human_time(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    minutes = seconds / 60
    if minutes < 90:
        return f"{minutes:.0f}m"
    return f"{minutes / 60:.1f}h"


def run_one(
    job: Job, case: str, threshold: float, workers: int, until: str | None,
    accept_low_quality: bool,
) -> tuple[str, str]:
    """Run the full pipeline for one PDF. Returns (outcome, detail).

    Outcome is "ok", "review" or "failed". Three rather than two because a file
    whose scan quality does not fit the threshold is neither: nothing broke, but
    it must not reach the index unexamined. Folding it into "failed" would bury
    it among real errors, and folding it into "ok" would silently index a
    document that is mostly discarded pages.

    Output is redirected to a per-file log rather than the console: at batch
    scale the page-by-page detail is thousands of lines, and it is only wanted
    when something needs attention.
    """
    log_dir = paths.ocr_dir(case, job.pdf.stem)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "ingest.log"

    cmd = [
        sys.executable, "-m", "src.ingestion.pipeline", str(job.pdf),
        "--case", case,
        "--threshold", str(threshold),
        "--workers", str(workers),
    ]
    if until:
        cmd += ["--until", until]
    if accept_low_quality:
        cmd += ["--accept-low-quality"]
    with log_path.open("w", encoding="utf-8") as log:
        result = subprocess.run(cmd, cwd=REPO_ROOT, stdout=log, stderr=subprocess.STDOUT)

    if result.returncode == 0:
        return "ok", ""
    if result.returncode == EXIT_NEEDS_REVIEW:
        # The reason is in the log; surfacing it in the summary saves opening
        # fifty logs to find the two that matter.
        return "review", read_review_reason(log_path)
    return "failed", f"exit {result.returncode}, see {log_path.relative_to(REPO_ROOT)}"


def read_review_reason(log_path: Path) -> str:
    """Pull the quality concerns out of a held file's log.

    score_pages prints them as bullet lines under a NEEDS REVIEW heading. Read
    back rather than passed through a return value because the stage runs as a
    subprocess two levels down, and its exit code carries no room for detail.
    """
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return f"see {log_path.name}"

    reasons: list[str] = []
    for i, line in enumerate(lines):
        if "NEEDS REVIEW" not in line:
            continue
        # The heading itself may carry the reason ("NEEDS REVIEW — every page
        # was discarded..."), or introduce a bulleted list of them. Both shapes
        # occur, so read whichever is present.
        _, _, tail = line.partition("—")
        if tail.strip():
            reasons.append(tail.strip().rstrip(","))
        for following in lines[i + 1:]:
            stripped = following.strip()
            if stripped.startswith("- "):
                reasons.append(stripped[2:].strip())
            elif reasons and not stripped:
                break
    return "; ".join(reasons) if reasons else f"see {log_path.name}"


def main() -> None:
    ap = argparse.ArgumentParser(description="Ingest a folder of PDFs.")
    ap.add_argument("paths", nargs="+", type=Path, help="PDF files or folders of PDFs")
    ap.add_argument(
        "--threshold",
        type=float,
        required=True,
        help="OCR-confidence cut for the score stage. Required in batch mode: "
             "without it the pipeline pauses for a human to inspect each file's "
             "distribution, which does not scale past a handful of documents.",
    )
    ap.add_argument(
        "--case",
        required=True,
        help="Case every PDF in this run belongs to, e.g. bundy. One flag for "
             "the folder: a batch mixing cases should be two runs, so that "
             "which case a document belongs to is never a guess.",
    )
    ap.add_argument("--workers", type=int, default=None, help="OCR workers per file")
    ap.add_argument(
        "--until",
        help="Stop each file after this stage. Passed through to the pipeline; "
             "use --until chunk to ingest without writing to the live index.",
    )
    ap.add_argument(
        "--accept-low-quality",
        action="store_true",
        help="Ingest files that trip a scan-quality check instead of holding them.",
    )
    ap.add_argument("--dry-run", action="store_true", help="Show the plan without running")
    args = ap.parse_args()

    try:
        case = validate_case(args.case)
    except ValueError as exc:
        sys.exit(str(exc))

    pdfs = discover(args.paths)
    if not pdfs:
        sys.exit("No PDFs found.")

    poppler_path = find_poppler_bin()
    jobs, unreadable = build_jobs(pdfs, poppler_path)
    for pdf, reason in unreadable:
        print(f"  cannot read {pdf.name}: {reason}")
    if not jobs:
        sys.exit("No readable PDFs found.")

    state_path = paths.batch_state_path(case)
    state = load_state(state_path)
    todo = [j for j in jobs if not is_complete(state, j, case)]
    skipped = len(jobs) - len(todo)

    # Largest first. If the longest document runs last, everything else finishes
    # and the batch waits on it alone; starting it first overlaps it with the
    # short ones. Free — it is a sort order.
    todo.sort(key=lambda j: j.pages, reverse=True)

    workers = args.workers or 1
    pages_todo = sum(j.pages for j in todo)

    print(f"{len(jobs)} files, {sum(j.pages for j in jobs):,} pages")
    print(f"  already done : {skipped} files")
    print(f"  to process   : {len(todo)} files, {pages_todo:,} pages")
    if todo:
        print(f"  estimated    : ~{human_time(estimate_seconds(pages_todo, workers))}"
              f" at {workers} worker{'s' if workers != 1 else ''}")

    if args.dry_run:
        print("\n(dry run — nothing executed)")
        for job in todo:
            print(f"  would process  {job.pdf.name:<34} {job.pages:>5} pages")
        return

    if not todo:
        print("\nNothing to do.")
        # Unreadable files still have to be reported and still have to fail the
        # run. Returning success here would mean a nightly batch whose only
        # remaining problem is a permanently corrupt file reports clean forever.
        if unreadable:
            print(f"  failed    : {len(unreadable)}")
            for pdf, reason in unreadable:
                print(f"    {pdf.name}  —  unreadable: {reason}")
            sys.exit(1)
        return

    print("-" * 72)
    started = time.time()
    succeeded, review, failed = [], [], []

    for i, job in enumerate(todo, 1):
        label = f"[{i}/{len(todo)}] {job.pdf.name:<34} {job.pages:>5}p"
        print(f"{label}  running...", flush=True)

        t0 = time.time()
        outcome, detail = run_one(
            job, case, args.threshold, workers, args.until, args.accept_low_quality
        )
        elapsed = time.time() - t0

        state[job.pdf.name] = {
            "status": outcome,
            "fingerprint": job.fingerprint,
            "pages": job.pages,
            "seconds": round(elapsed, 1),
            "detail": detail,
        }
        # Saved after every file, not at the end. A batch killed halfway must
        # still know what it finished.
        save_state(state_path, state)

        if outcome == "ok":
            succeeded.append(job)
            print(f"{label}  ok in {human_time(elapsed)}")
        elif outcome == "review":
            review.append((job, detail))
            print(f"{label}  NEEDS REVIEW — {detail}")
        else:
            failed.append((job, detail))
            print(f"{label}  FAILED — {detail}")

    print("-" * 72)
    print(f"Done in {human_time(time.time() - started)}.")
    print(f"  ingested     : {len(succeeded)}")
    print(f"  skipped      : {skipped}  (already complete)")
    print(f"  needs review : {len(review)}")
    for job, detail in review:
        print(f"    {job.pdf.name}  —  {detail}")
    print(f"  failed       : {len(failed) + len(unreadable)}")
    for job, detail in failed:
        print(f"    {job.pdf.name}  —  {detail}")
    for pdf, reason in unreadable:
        print(f"    {pdf.name}  —  unreadable: {reason}")

    if review:
        plural = "s" if len(review) != 1 else ""
        # Suggesting the override to someone who already passed it would be
        # useless advice: what remains held under --accept-low-quality is held
        # because it has no usable pages at all, which only a lower threshold
        # or dropping the file can fix.
        remedy = (
            "Lower the threshold for those files, or leave them out."
            if args.accept_low_quality
            else "Re-run with a threshold suited to them, or --accept-low-quality "
                 "to ingest as scored."
        )
        print(
            f"\nNothing was written to the index for the {len(review)} held "
            f"file{plural}. {remedy}"
        )

    # Three exit codes for the three outcomes, so a scheduled run can tell them
    # apart without parsing the summary: 1 means something broke and wants
    # fixing, 2 means nothing broke but a person has to look before those files
    # can be indexed. Failure wins when both happened — it is the more urgent
    # of the two.
    if failed or unreadable:
        sys.exit(1)
    if review:
        sys.exit(EXIT_NEEDS_REVIEW)


if __name__ == "__main__":
    main()
