"""
Generic merge: roll up a parent teletype + its inline 4-750 deletion sheets +
its absorbed continuation docs into one document, the same way we did for the
Denver teletype (doc-017).

Drives two merges configured in MERGES below — extend the list to run more.

Idempotent: each merge is a no-op if the parent doc already has the expected
page count.
"""

import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

JSONL = Path(r"c:\Learning\Case_File_AI\data\ocr\bundy-part-01\pages.jsonl")
DOCS  = Path(r"c:\Learning\Case_File_AI\data\ocr\bundy-part-01\docs.jsonl")

PLACEHOLDER_TEXT = "[WITHHELD - FOIA 4-750]"

# Each merge = (parent_doc, [absorbed_docs incl parent], {pages with inline placeholder}, note)
MERGES = [
    {
        "parent": "bundy-part-01__doc-041",
        "absorbed": [
            "bundy-part-01__doc-041",   # body  (p61)
            "bundy-part-01__doc-042",   # 4-750 (p62)
            "bundy-part-01__doc-043",   # LEADS (p63)
        ],
        "placeholders": {62},
        "kind": "teletype",
        "first_template": "unknown",
        "note": (
            "Merged from doc-041 + doc-042 + doc-043. "
            "Salt Lake / Denver teletype on the Grand Junction RA telephone "
            "call (case#s 88-6895 / 88-10975). p62 is an inline FOIA 4-750."
        ),
    },
    {
        "parent": "bundy-part-01__doc-049",
        "absorbed": [
            "bundy-part-01__doc-049",   # header+body (p77, dated 1/4/78)
            "bundy-part-01__doc-050",   # 4-750       (p78)
            "bundy-part-01__doc-051",   # LEADS+close (p79)
        ],
        "placeholders": {78},
        "kind": "teletype",
        "first_template": "unknown",
        "note": (
            "Merged from doc-049 + doc-050 + doc-051. "
            "Salt Lake -> SAC Denver teletype dated 1/4/78 (case#s 88-6895 / "
            "88-10975). p78 is an inline FOIA 4-750."
        ),
    },
]


def load() -> tuple[list[dict], list[dict]]:
    pages = [json.loads(line) for line in JSONL.open(encoding="utf-8") if line.strip()]
    docs  = [json.loads(line) for line in DOCS.open(encoding="utf-8") if line.strip()]
    return pages, docs


def save(pages: list[dict], docs: list[dict]) -> None:
    with JSONL.open("w", encoding="utf-8") as f:
        for p in pages:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    with DOCS.open("w", encoding="utf-8") as f:
        for d in docs:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")


def apply_merge(pages: list[dict], docs: list[dict], spec: dict) -> bool:
    """Apply one merge in place. Return True if applied, False if no-op."""
    parent_id = spec["parent"]
    absorbed  = set(spec["absorbed"])
    placeholders = spec["placeholders"]

    # Pull the parent's existing entry to gate idempotency
    parent_entry = next((d for d in docs if d["doc_id"] == parent_id), None)
    if parent_entry is None:
        print(f"  SKIP {parent_id}: parent not found (already merged or invalid id)")
        return False

    absorbed_page_nos = sorted(
        p["page_no"] for p in pages if p.get("doc_id") in absorbed
    )
    expected = absorbed_page_nos

    if len(parent_entry["page_nos"]) == len(expected) and parent_entry["page_nos"] == expected:
        print(f"  SKIP {parent_id}: already at {len(expected)} pages, merge appears applied")
        return False

    # Update each page row.
    new_count = len(expected)
    for idx, pn in enumerate(expected, start=1):
        page = next(p for p in pages if p["page_no"] == pn)
        page["doc_id"] = parent_id
        page["doc_kind"] = spec["kind"]
        page["doc_page_index"] = idx
        page["doc_page_count"] = new_count
        if pn in placeholders:
            page["inline_placeholder"] = PLACEHOLDER_TEXT
        else:
            page.pop("inline_placeholder", None)

    # Aggregate case numbers across all absorbed source docs.
    case_nums: set[str] = set()
    for d in docs:
        if d["doc_id"] in absorbed:
            case_nums.update(d.get("case_nums", []))

    merged = {
        "doc_id": parent_id,
        "kind": spec["kind"],
        "page_nos": expected,
        "page_count": new_count,
        "first_template": spec["first_template"],
        "case_nums": sorted(case_nums),
        "withheld_page_nos": sorted(placeholders),
        "note": spec["note"],
    }

    # Rewrite docs list: replace parent in place, drop other absorbed entries.
    new_docs: list[dict] = []
    for d in docs:
        if d["doc_id"] == parent_id:
            new_docs.append(merged)
        elif d["doc_id"] in absorbed:
            continue
        else:
            new_docs.append(d)
    docs[:] = new_docs

    removed = sorted(absorbed - {parent_id})
    print(f"  MERGED into {parent_id}")
    print(f"    pages: {expected}  ({new_count} pages)")
    print(f"    removed: {', '.join(removed)}")
    print(f"    inline placeholders on: {sorted(placeholders)}")
    return True


def main() -> None:
    pages, docs = load()
    any_change = False
    for spec in MERGES:
        print(f"Applying merge -> {spec['parent']}")
        if apply_merge(pages, docs, spec):
            any_change = True
        print()

    if not any_change:
        print("No changes. Files left untouched.")
        return

    save(pages, docs)
    print(f"Wrote {JSONL.name} and {DOCS.name}")
    print(f"docs.jsonl now contains {len(docs)} documents")


if __name__ == "__main__":
    main()
