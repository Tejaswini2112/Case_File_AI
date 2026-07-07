"""
Quick investigation tool: show the first 5 non-blank lines of every page that
template detection couldn't classify.

Helps answer: are these teletype continuation pages, garbled form numbers, or
something genuinely new?

Usage (run from project root):
    python scripts/inspect_unknowns.py data/ocr/bundy-part-01/pages.jsonl
"""

import json
import sys
from pathlib import Path

# Make src/ importable so we can reuse the template detector from the cleaner.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.stdout.reconfigure(encoding="utf-8")

from src.ingestion.clean_pages import detect_template


def main(jsonl_path: Path) -> None:
    if not jsonl_path.exists():
        sys.exit(f"Not found: {jsonl_path}")

    with jsonl_path.open(encoding="utf-8") as f:
        pages = [json.loads(line) for line in f if line.strip()]

    unknowns = []
    for p in pages:
        if p.get("bucket") != "clean":
            continue
        if detect_template(p["raw_text"]) == "unknown":
            unknowns.append(p)

    print(f"Found {len(unknowns)} unknown clean-bucket pages.")
    print("=" * 72)

    for p in unknowns:
        lines = [line.strip() for line in p["raw_text"].splitlines() if line.strip()]
        head = lines[:5]
        print(f"\nPAGE {p['page_no']:>3}  (conf={p['ocr_confidence']:.1f}, chars={p['char_count']})")
        for line in head:
            print(f"   {line[:90]}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("Usage: python scripts/inspect_unknowns.py <path-to-pages.jsonl>")
    main(Path(sys.argv[1]))
