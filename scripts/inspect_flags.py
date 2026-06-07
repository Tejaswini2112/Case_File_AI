"""
Quick investigation tool: show every page that tripped the fact-preservation
guardrail, with each lost fact shown alongside its raw-text context so we
can tell boilerplate false-flags from real case-fact losses.

Usage (run from project root):
    python scripts/inspect_flags.py data/ocr/bundy-part-01/pages.jsonl
"""

import json
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")


def context_around(text: str, needle: str, span: int = 40) -> str:
    """Return ~span chars on either side of needle's first occurrence."""
    idx = text.find(needle)
    if idx == -1:
        return "(not found in raw)"
    start = max(0, idx - span)
    end = min(len(text), idx + len(needle) + span)
    snippet = text[start:end].replace("\n", " ")
    return f"...{snippet}..."


def main(jsonl_path: Path) -> None:
    with jsonl_path.open(encoding="utf-8") as f:
        pages = [json.loads(line) for line in f if line.strip()]

    flagged = [p for p in pages if p.get("cleaning_flags")]
    print(f"Found {len(flagged)} flagged pages out of {len(pages)} total.")
    print("=" * 72)

    for p in flagged:
        print(f"\nPAGE {p['page_no']:>3}  template={p.get('template')}  "
              f"template_confidence={p.get('template_confidence')}  "
              f"conf={p['ocr_confidence']:.1f}")
        for flag in p["cleaning_flags"]:
            print(f"  {flag['type']}:")
            for fact in flag["lost"]:
                ctx = context_around(p["raw_text"], fact)
                print(f"    {fact!r:>20}  ->  {ctx}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("Usage: python scripts/inspect_flags.py <path-to-pages.jsonl>")
    main(Path(sys.argv[1]))
