"""
One-off manual merge: the long Denver teletype was split into three docs by
the linear-scan grouper because 4-750 deletion sheets sit between its
continuation pages. The 4-750s are FOIA withholdings — semantically part of
the SAME teletype, just with body redacted. This pass merges them.

  doc-017  pages [26,27,28,29]   teletype           ← parent (keeps doc_id)
  doc-018  pages [30]            deletion-sheet     ← absorbed inline
  doc-019  pages [31,33]         teletype-cont      ← absorbed (orphan label cleared)
  doc-020  pages [34]            deletion-sheet     ← absorbed inline
  doc-021  pages [35,36]         loose              ← absorbed

After merge:
  doc-017  pages [26,27,28,29,30,31,33,34,35,36]   teletype

The two 4-750 pages (30, 34) keep their original extracted_metadata
(exemptions_cited etc.) but are flagged with `inline_placeholder` so the
chunker emits "[WITHHELD - FOIA 4-750]" in place of their raw body.

Page 32 (bucket=skipped, unusable OCR) is not assigned to the merged doc,
matching how skipped pages were treated by the original grouping pass.

Idempotent: re-running the script is a no-op if the merge already happened.
"""

import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

JSONL = Path(r"c:\Learning\Case_File_AI\data\ocr\bundy-part-01\pages.jsonl")
DOCS  = Path(r"c:\Learning\Case_File_AI\data\ocr\bundy-part-01\docs.jsonl")

PARENT_DOC = "bundy-part-01__doc-017"
ABSORBED_DOCS = {
    "bundy-part-01__doc-017",  # parent itself, included for symmetry
    "bundy-part-01__doc-018",  # 4-750 deletion sheet (page 30)
    "bundy-part-01__doc-019",  # teletype-cont orphan (pages 31, 33)
    "bundy-part-01__doc-020",  # 4-750 deletion sheet (page 34)
    "bundy-part-01__doc-021",  # loose continuation (pages 35, 36)
}
PLACEHOLDER_PAGE_NOS = {30, 34}
PLACEHOLDER_TEXT = "[WITHHELD - FOIA 4-750]"


def main() -> None:
    pages = [json.loads(line) for line in JSONL.open(encoding="utf-8") if line.strip()]
    docs  = [json.loads(line) for line in DOCS.open(encoding="utf-8") if line.strip()]

    # Idempotency check — has the merge already happened?
    parent = next((d for d in docs if d["doc_id"] == PARENT_DOC), None)
    if parent and len(parent["page_nos"]) > 4:
        print(f"Merge appears already applied — {PARENT_DOC} has {len(parent['page_nos'])} pages. Exiting.")
        return

    # Collect every page currently attached to any of the absorbed docs.
    absorbed_pages = sorted(
        p["page_no"] for p in pages if p.get("doc_id") in ABSORBED_DOCS
    )
    print(f"Pages being merged into {PARENT_DOC}: {absorbed_pages}")

    # Update the pages.jsonl rows.
    new_count = len(absorbed_pages)
    for idx, pn in enumerate(absorbed_pages, start=1):
        page = next(p for p in pages if p["page_no"] == pn)
        page["doc_id"] = PARENT_DOC
        page["doc_kind"] = "teletype"
        page["doc_page_index"] = idx
        page["doc_page_count"] = new_count
        if pn in PLACEHOLDER_PAGE_NOS:
            page["inline_placeholder"] = PLACEHOLDER_TEXT
        else:
            # Clear stale placeholder if any
            page.pop("inline_placeholder", None)

    # Rewrite docs.jsonl: drop absorbed entries except the parent; replace
    # the parent with the merged version.
    case_nums: set[str] = set()
    for d in docs:
        if d["doc_id"] in ABSORBED_DOCS:
            case_nums.update(d.get("case_nums", []))

    merged_parent = {
        "doc_id": PARENT_DOC,
        "kind": "teletype",
        "page_nos": absorbed_pages,
        "page_count": new_count,
        "first_template": "unknown",   # p26 was the OCR-garbled first page
        "case_nums": sorted(case_nums),
        "withheld_page_nos": sorted(PLACEHOLDER_PAGE_NOS),
        "note": (
            "Merged from doc-017 + doc-018 + doc-019 + doc-020 + doc-021. "
            "Pages 30 and 34 are inline FOIA 4-750 withholdings."
        ),
    }

    new_docs: list[dict] = []
    for d in docs:
        if d["doc_id"] == PARENT_DOC:
            new_docs.append(merged_parent)
        elif d["doc_id"] in ABSORBED_DOCS:
            continue
        else:
            new_docs.append(d)

    # Write back.
    with JSONL.open("w", encoding="utf-8") as f:
        for p in pages:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    with DOCS.open("w", encoding="utf-8") as f:
        for d in new_docs:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")

    removed = sorted(ABSORBED_DOCS - {PARENT_DOC})
    print(f"\nRewrote {JSONL.name} and {DOCS.name}")
    print(f"  Parent doc {PARENT_DOC} now spans {new_count} pages")
    print(f"  Removed from docs.jsonl: {', '.join(removed)}")
    print(f"  Inline placeholders set on pages: {sorted(PLACEHOLDER_PAGE_NOS)}")


if __name__ == "__main__":
    main()
