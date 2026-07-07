"""
Step 2 — Look at the per-page OCR score distribution and pick a threshold.

Reads:  data/ocr/<name>/pages.jsonl   (produced by ocr.py)
Prints: stats summary + ASCII histograms of confidence and letter ratio
        + a list of pages that sit near common threshold candidates
        (for spot-checking before committing to a cut).

The threshold lives in your head for now. Once you've picked one, re-run
with --threshold N to write bucket assignments back to pages.jsonl.

Usage (run from project root):
    python src/score_pages.py data/ocr/bundy-part-01/pages.jsonl
    python src/score_pages.py data/ocr/bundy-part-01/pages.jsonl --threshold 55
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

BUCKETS = ("clean", "skipped")


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


if __name__ == "__main__":
    main()