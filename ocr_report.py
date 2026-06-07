"""
OCR quality report — how well did the OCR pass go, and what needs rework?

Reads:  data/ocr/<name>/pages.jsonl   (produced by ocr.py)
Writes: data/ocr/<name>/ocr_report.md   human-readable quality report
        data/ocr/<name>/ocr_report.csv  per-page metrics for spreadsheets
Prints: a short summary + the list of pages flagged for further processing.

This is a read-only diagnostic: it never touches pages.jsonl or the page text.
It classifies each page into a quality tier from the metrics ocr.py already
captured (confidence, ASCII letter ratio, char count) so you can see at a
glance whether the scan is good enough to chunk or needs a second look.

Usage:
    python ocr_report.py data/ocr/bundy-part-01/pages.jsonl
"""

import csv
import json
import statistics as stats
import sys
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

# Quality tiers, checked top to bottom; first match wins.
# These mirror score_pages.py's clean-rule (conf >= threshold AND letters >= 0.55)
# but split the pass/fail line into finer tiers so the report is more diagnostic.
LETTER_FLOOR = 0.55          # below this the page is symbol-soup regardless of confidence
NEAR_EMPTY_CHARS = 50        # fewer chars than this -> likely blank / image-only page

GOOD_CONF = 85.0             # confident, clean text -> ready to use
OK_CONF = 70.0               # solid, minor noise
REVIEW_CONF = 50.0           # marginal -> eyeball before trusting
# anything below REVIEW_CONF (or below the letter floor) -> POOR -> needs rework


def classify(page: dict) -> tuple[str, str]:
    """Return (tier, reason). Tier is one of GOOD / OK / REVIEW / POOR / EMPTY."""
    conf = page["ocr_confidence"]
    ratio = page["letter_ratio"]
    chars = page["char_count"]

    if chars < NEAR_EMPTY_CHARS:
        return "EMPTY", f"only {chars} chars — blank, image-only, or failed render"
    if ratio < LETTER_FLOOR:
        return "POOR", f"letter ratio {ratio:.0%} < {LETTER_FLOOR:.0%} — symbol soup / heavy redaction"
    if conf < REVIEW_CONF:
        return "POOR", f"confidence {conf:.1f} < {REVIEW_CONF:.0f} — low-quality recognition"
    if conf < OK_CONF:
        return "REVIEW", f"confidence {conf:.1f} in {REVIEW_CONF:.0f}-{OK_CONF:.0f} — marginal, spot-check"
    if conf < GOOD_CONF:
        return "OK", f"confidence {conf:.1f} — solid with minor noise"
    return "GOOD", f"confidence {conf:.1f} — clean"


def load_pages(jsonl_path: Path) -> list[dict]:
    with jsonl_path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def summarize(values: list[float]) -> dict:
    return {
        "min": min(values),
        "max": max(values),
        "mean": stats.mean(values),
        "median": stats.median(values),
        "stdev": stats.pstdev(values),
    }


def write_csv(pages: list[dict], csv_path: Path) -> None:
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            ["page_no", "tier", "ocr_confidence", "letter_ratio",
             "char_count", "redaction_markers", "reason"]
        )
        for p in pages:
            tier, reason = classify(p)
            w.writerow([
                p["page_no"], tier, p["ocr_confidence"], p["letter_ratio"],
                p["char_count"], " ".join(p["redaction_markers_found"]), reason,
            ])


def write_markdown(pages: list[dict], jsonl_path: Path, md_path: Path) -> dict:
    """Write the Markdown report. Returns the tier counts for the console summary."""
    confs = [p["ocr_confidence"] for p in pages]
    ratios = [p["letter_ratio"] for p in pages]
    chars = [p["char_count"] for p in pages]

    c = summarize(confs)
    r = summarize(ratios)
    h = summarize([float(x) for x in chars])

    tiers = {t: [] for t in ("GOOD", "OK", "REVIEW", "POOR", "EMPTY")}
    for p in pages:
        tier, reason = classify(p)
        tiers[tier].append((p, reason))

    n = len(pages)
    redacted = [p for p in pages if p["redaction_markers_found"]]
    flagged = tiers["REVIEW"] + tiers["POOR"] + tiers["EMPTY"]
    usable = len(tiers["GOOD"]) + len(tiers["OK"])

    lines: list[str] = []
    lines.append(f"# OCR Quality Report — {jsonl_path.parent.name}")
    lines.append("")
    lines.append(f"_Generated {datetime.now():%Y-%m-%d %H:%M} from `{jsonl_path}`_")
    lines.append("")

    # --- Verdict --------------------------------------------------------
    pct_usable = usable / n * 100
    if pct_usable >= 80:
        verdict = f"**Healthy.** {usable}/{n} pages ({pct_usable:.0f}%) are usable as-is."
    elif pct_usable >= 60:
        verdict = f"**Mixed.** {usable}/{n} pages ({pct_usable:.0f}%) usable; a meaningful tail needs attention."
    else:
        verdict = f"**Needs work.** Only {usable}/{n} pages ({pct_usable:.0f}%) usable — consider re-scanning or re-OCR."
    lines.append("## Verdict")
    lines.append("")
    lines.append(verdict)
    lines.append("")

    # --- Summary stats --------------------------------------------------
    lines.append("## Summary metrics")
    lines.append("")
    lines.append("| Metric | Min | Median | Mean | Max | Std dev |")
    lines.append("|---|---|---|---|---|---|")
    lines.append(f"| OCR confidence | {c['min']:.1f} | {c['median']:.1f} | {c['mean']:.1f} | {c['max']:.1f} | {c['stdev']:.1f} |")
    lines.append(f"| ASCII letter ratio | {r['min']:.0%} | {r['median']:.0%} | {r['mean']:.0%} | {r['max']:.0%} | {r['stdev']:.0%} |")
    lines.append(f"| Char count | {h['min']:.0f} | {h['median']:.0f} | {h['mean']:.0f} | {h['max']:.0f} | {h['stdev']:.0f} |")
    lines.append("")
    lines.append(f"- **Pages with redaction markers:** {len(redacted)} "
                 f"({len(redacted)/n*100:.0f}%), {sum(len(p['redaction_markers_found']) for p in redacted)} total hits")
    lines.append("")

    # --- Quality breakdown ----------------------------------------------
    lines.append("## Quality breakdown")
    lines.append("")
    lines.append("| Tier | Meaning | Pages | Share |")
    lines.append("|---|---|---|---|")
    meanings = {
        "GOOD": f"conf ≥ {GOOD_CONF:.0f}, clean — ready to chunk",
        "OK": f"conf {OK_CONF:.0f}–{GOOD_CONF:.0f} — usable, minor noise",
        "REVIEW": f"conf {REVIEW_CONF:.0f}–{OK_CONF:.0f} — spot-check",
        "POOR": f"conf < {REVIEW_CONF:.0f} or letters < {LETTER_FLOOR:.0%} — rework",
        "EMPTY": f"< {NEAR_EMPTY_CHARS} chars — blank / image-only",
    }
    for t in ("GOOD", "OK", "REVIEW", "POOR", "EMPTY"):
        cnt = len(tiers[t])
        lines.append(f"| {t} | {meanings[t]} | {cnt} | {cnt/n*100:.0f}% |")
    lines.append("")

    # --- Pages needing attention ----------------------------------------
    lines.append("## Pages flagged for further processing")
    lines.append("")
    if not flagged:
        lines.append("None — every page cleared the REVIEW bar. 🎉")
    else:
        lines.append(f"{len(flagged)} page(s), worst first. These drive the decision on re-OCR / re-scan.")
        lines.append("")
        lines.append("| Page | Tier | Conf | Letters | Chars | Redactions | Why |")
        lines.append("|---|---|---|---|---|---|---|")
        order = {"EMPTY": 0, "POOR": 1, "REVIEW": 2}
        flagged.sort(key=lambda pr: (order[classify(pr[0])[0]], pr[0]["ocr_confidence"]))
        for p, reason in flagged:
            mk = " ".join(p["redaction_markers_found"]) or "—"
            tier, _ = classify(p)
            lines.append(
                f"| {p['page_no']} | {tier} | {p['ocr_confidence']:.1f} | "
                f"{p['letter_ratio']:.0%} | {p['char_count']} | {mk} | {reason} |"
            )
    lines.append("")

    # --- Full per-page table --------------------------------------------
    lines.append("## All pages")
    lines.append("")
    lines.append("| Page | Tier | Conf | Letters | Chars | Redactions |")
    lines.append("|---|---|---|---|---|---|")
    for p in pages:
        tier, _ = classify(p)
        mk = " ".join(p["redaction_markers_found"]) or "—"
        lines.append(
            f"| {p['page_no']} | {tier} | {p['ocr_confidence']:.1f} | "
            f"{p['letter_ratio']:.0%} | {p['char_count']} | {mk} |"
        )
    lines.append("")

    md_path.write_text("\n".join(lines), encoding="utf-8")
    return {t: len(v) for t, v in tiers.items()}


def main(jsonl_path: Path) -> None:
    if not jsonl_path.exists():
        sys.exit(f"Not found: {jsonl_path}")

    pages = load_pages(jsonl_path)
    if not pages:
        sys.exit(f"No pages in {jsonl_path}")

    out_dir = jsonl_path.parent
    md_path = out_dir / "ocr_report.md"
    csv_path = out_dir / "ocr_report.csv"

    counts = write_markdown(pages, jsonl_path, md_path)
    write_csv(pages, csv_path)

    n = len(pages)
    usable = counts["GOOD"] + counts["OK"]
    flagged = counts["REVIEW"] + counts["POOR"] + counts["EMPTY"]

    print("=" * 60)
    print(f"OCR quality report — {out_dir.name}  ({n} pages)")
    print("=" * 60)
    for t in ("GOOD", "OK", "REVIEW", "POOR", "EMPTY"):
        cnt = counts[t]
        bar = "#" * round(cnt / n * 40)
        print(f"  {t:<7} {cnt:>3}  {cnt/n*100:>3.0f}%  {bar}")
    print("-" * 60)
    print(f"  usable (GOOD+OK):   {usable:>3}  ({usable/n*100:.0f}%)")
    print(f"  flagged for rework: {flagged:>3}  ({flagged/n*100:.0f}%)")
    print("-" * 60)
    print(f"Wrote {md_path}")
    print(f"Wrote {csv_path}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("Usage: python ocr_report.py <path-to-pages.jsonl>")
    main(Path(sys.argv[1]))
