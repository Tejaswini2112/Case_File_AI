"""
Step 2 — Look at the per-page OCR score distribution and pick a threshold.

Reads:  data/cases/<case>/ocr/<name>/pages.jsonl   (produced by ocr.py)
Prints: stats summary + ASCII histograms of confidence and letter ratio
        + a list of pages that sit near common threshold candidates
        (for spot-checking before committing to a cut).

The threshold lives in your head for now. Once you've picked one, re-run
with --threshold N to write bucket assignments back to pages.jsonl.

Usage (run from project root):
    python src/score_pages.py data/cases/bundy/ocr/bundy-part-01/pages.jsonl
    python src/score_pages.py data/cases/bundy/ocr/bundy-part-01/pages.jsonl --threshold 55
"""

import argparse
import json
import statistics
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

BUCKETS = ("clean", "skipped")

# Exit code meaning "this file needs a human, but nothing is broken".
#
# A third outcome is needed because a scan that does not fit the threshold is
# neither a success nor a failure. Reporting it as a failure would bury it among
# genuine errors; reporting it as success would index a document that is mostly
# discarded pages without anyone noticing.
EXIT_NEEDS_REVIEW = 2

# Flag a file when more than this share of its pages would be thrown away.
#
# Calibrated against the corpus rather than guessed: the three Bundy releases
# discard 24%, 22% and 20% of their pages at a threshold of 60, so a fifth is
# normal for this scanning generation. Half is well outside that band, and means
# either the scan is unusually poor or the threshold is wrong for this document
# -- both of which want a person to look before the file reaches the index.
MAX_SKIPPED_RATIO = 0.5


def load_pages(jsonl_path: Path) -> list[dict]:
    with jsonl_path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def histogram(values: list[float], bin_size: int, max_value: int, label: str) -> None:
    """Print an ASCII histogram. values are 0..max_value."""
    bins: Counter[int] = Counter()
    for v in values:
        bucket = min(int(v // bin_size) * bin_size, max_value - bin_size)
        bins[bucket] += 1

    max_count = max(bins.values()) if bins else 1
    width = 40  # max bar width in chars

    print(f"\n{label}  (n={len(values)})")
    print("-" * 60)
    for low in range(0, max_value, bin_size):
        high = low + bin_size
        count = bins.get(low, 0)
        bar = "#" * round(count / max_count * width)
        print(f"  {low:>3}-{high-1:<3}  {count:>3}  {bar}")


def show_pages_near(pages: list[dict], center: int, span: int = 10) -> None:
    """List pages whose confidence is within ±span of `center`. For spot-checking."""
    near = [p for p in pages if abs(p["ocr_confidence"] - center) <= span]
    near.sort(key=lambda p: p["ocr_confidence"])
    if not near:
        print(f"  (no pages within ±{span} of {center})")
        return
    for p in near:
        print(
            f"  page {p['page_no']:>3}  "
            f"conf={p['ocr_confidence']:>5.1f}  "
            f"letters={p['letter_ratio']:>5.1%}  "
            f"chars={p['char_count']:>5}"
        )


def assign_buckets(pages: list[dict], threshold: float) -> tuple[int, int]:
    """
    Apply the two-bucket rule:
        clean   = ocr_confidence >= threshold AND letter_ratio >= 0.55
        skipped = everything else

    Letter-ratio acts as the cheap second check that catches symbol-soup pages
    where Tesseract was confident but the input was junk (e.g. heavy redaction).
    """
    LETTER_FLOOR = 0.55
    clean = 0
    skipped = 0
    for p in pages:
        is_clean = p["ocr_confidence"] >= threshold and p["letter_ratio"] >= LETTER_FLOOR
        p["bucket"] = "clean" if is_clean else "skipped"
        if is_clean:
            clean += 1
        else:
            skipped += 1
    return clean, skipped


def quality_concerns(pages: list[dict], threshold: float, skipped: int) -> list[str]:
    """Reasons this file should not be indexed without someone looking first.

    Two checks, both self-contained — neither needs a corpus-wide baseline,
    so they work on the first file ever ingested as well as the thousandth.

    Deliberately not a per-file threshold. The threshold is an absolute quality
    bar: text this garbled is not worth indexing regardless of what else is in
    the document. Making it relative — keeping the best 80% of each file — would
    rank rather than judge, so a uniformly terrible scan would keep its
    least-terrible pages and a pristine one would discard good pages for nothing.
    The bar stays fixed; what varies is whether we trust it for this file.
    """
    concerns = []
    ratio = skipped / len(pages) if pages else 1.0
    if ratio > MAX_SKIPPED_RATIO:
        concerns.append(
            f"{ratio:.0%} of pages below threshold {threshold:g} "
            f"(normal for this corpus is around 20%)"
        )

    median = statistics.median(p["ocr_confidence"] for p in pages)
    if median < threshold:
        concerns.append(
            f"median confidence {median:.1f} is below threshold {threshold:g}, "
            f"so the typical page in this file fails the bar"
        )
    return concerns


def write_bucketed(pages: list[dict], jsonl_path: Path) -> None:
    """Overwrite pages.jsonl with the bucket field populated."""
    with jsonl_path.open("w", encoding="utf-8") as f:
        for p in pages:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("jsonl_path", type=Path)
    ap.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="OCR confidence threshold. If set, writes bucket assignments back to the JSONL.",
    )
    ap.add_argument(
        "--accept-low-quality",
        action="store_true",
        help="Continue even when the file trips a quality check. For when you "
             "have looked and decided it is fine — the gate is a speed bump, "
             "not a wall.",
    )
    args = ap.parse_args()

    if not args.jsonl_path.exists():
        sys.exit(f"Not found: {args.jsonl_path}")

    pages = load_pages(args.jsonl_path)
    confs = [p["ocr_confidence"] for p in pages]
    ratios = [p["letter_ratio"] * 100 for p in pages]  # scale to 0..100 for the histogram

    print("=" * 60)
    print(f"Page-quality distribution for {args.jsonl_path}")
    print("=" * 60)
    print(f"\nPages: {len(pages)}")
    print(f"Confidence: min={min(confs):.1f}  max={max(confs):.1f}  mean={sum(confs)/len(confs):.1f}")
    print(f"Letter ratio: min={min(ratios):.0f}%  max={max(ratios):.0f}%  mean={sum(ratios)/len(ratios):.0f}%")

    histogram(confs, bin_size=10, max_value=100, label="OCR confidence")
    histogram(ratios, bin_size=10, max_value=100, label="ASCII letter ratio (%)")

    if args.threshold is None:
        print("\n" + "=" * 60)
        print("Pages near common threshold candidates (for spot-checking):")
        print("=" * 60)
        for center in (50, 60, 70):
            print(f"\nNear confidence {center} (±5):")
            show_pages_near(pages, center, span=5)

        print("\n" + "=" * 60)
        print("Next step: pick a threshold and re-run with --threshold N")
        print("  e.g.  python score_pages.py", args.jsonl_path, "--threshold 60")
        print("=" * 60)
    else:
        clean, skipped = assign_buckets(pages, args.threshold)
        write_bucketed(pages, args.jsonl_path)
        print("\n" + "=" * 60)
        print(f"Applied threshold: ocr_confidence >= {args.threshold} AND letter_ratio >= 0.55")
        print(f"  clean:   {clean:>3} pages  -> will be chunked")
        print(f"  skipped: {skipped:>3} pages  -> set aside (raw OCR preserved)")
        print(f"\nWrote bucket assignments back to {args.jsonl_path}")
        print("=" * 60)

        # Buckets are written before the gate runs, deliberately. A file held
        # for review is more useful with its bucket assignments in place —
        # that is what a reviewer needs to look at — and nothing downstream
        # runs unless the pipeline continues.

        # Zero clean pages is not a judgement call, so --accept-low-quality does
        # not apply. There is literally nothing to chunk: group_documents would
        # assign no doc_ids and chunk_documents would fail with an error about
        # missing fields, several stages downstream of the real cause. Saying so
        # here points at the actual problem.
        if clean == 0:
            print(
                f"\nNEEDS REVIEW — every page was discarded at threshold "
                f"{args.threshold:g}, so there is nothing to ingest.\n"
                f"Lower the threshold, or leave this file out."
            )
            sys.exit(EXIT_NEEDS_REVIEW)

        concerns = quality_concerns(pages, args.threshold, skipped)
        if concerns and not args.accept_low_quality:
            print("\nNEEDS REVIEW — not continuing to the index:")
            for concern in concerns:
                print(f"  - {concern}")
            print(
                "\nLook at the histogram above, then either re-run with a "
                "threshold suited to this file\nor pass --accept-low-quality "
                "to ingest it as scored."
            )
            sys.exit(EXIT_NEEDS_REVIEW)
        if concerns:
            print("\n--accept-low-quality: continuing despite:")
            for concern in concerns:
                print(f"  - {concern}")


if __name__ == "__main__":
    main()