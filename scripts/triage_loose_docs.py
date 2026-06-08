"""
One-off triage of the 13 remaining `loose` docs in bundy-part-01.

Two steps:

  1. Reclassify each loose doc to a real kind (form, newspaper, teletype,
     legal, memo). Updates `kind` on docs.jsonl rows AND `doc_kind` on the
     page rows in pages.jsonl.
  2. Merge doc-013 + doc-014 into one newspaper document (same Salt Lake
     Tribune article spread across two pages).

Idempotent: rerunning is a no-op if kinds already match and the merge has
already happened.
"""

import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

JSONL = Path(r"c:\Learning\Case_File_AI\data\ocr\bundy-part-01\pages.jsonl")
DOCS  = Path(r"c:\Learning\Case_File_AI\data\ocr\bundy-part-01\docs.jsonl")

# --- Step 1: reclassifications ---
# `airtel` (doc-024) is folded into `teletype` as agreed.
RECLASSIFY: dict[str, str] = {
    "bundy-part-01__doc-003": "form",         # Bulky Exhibit Inventory
    "bundy-part-01__doc-006": "legal",        # Federal Complaint, Title 18 §1073
    "bundy-part-01__doc-012": "form",         # INS Lookout Notice (FD-315)
    "bundy-part-01__doc-013": "newspaper",    # SL Tribune "Leaps Out Window"
    "bundy-part-01__doc-014": "newspaper",    # continuation of doc-013 (pre-merge)
    "bundy-part-01__doc-016": "newspaper",    # newspaper continuation
    "bundy-part-01__doc-023": "form",         # 2nd INS Lookout Notice
    "bundy-part-01__doc-024": "teletype",     # FBI airtel SAC Denver -> SAC SLC
    "bundy-part-01__doc-025": "legal",        # Federal Warrant of Arrest
    "bundy-part-01__doc-027": "newspaper",    # "Former Law Student" article
    "bundy-part-01__doc-032": "memo",         # Optional Form 10 internal memo
    "bundy-part-01__doc-035": "legal",        # Federal Motion & Order for Dismissal
    "bundy-part-01__doc-038": "form",         # FD-LS9 Record of Info Furnished
}

# --- Step 2: doc-013 + doc-014 merge ---
MERGE = {
    "parent": "bundy-part-01__doc-013",
    "absorbed": [
        "bundy-part-01__doc-013",   # p21, the article's FD-350 mount
        "bundy-part-01__doc-014",   # p22, narrative continuation
    ],
    "placeholders": set(),          # no FOIA withholdings between them
    "kind": "newspaper",
    "first_template": "unknown",    # p21's template was missed by the detector
    "note": (
        "Merged from doc-013 + doc-014. Salt Lake Tribune article "
        "'Leaps Out Window, Escapes Into Hills' (Title: 'Bundy Escape', "
        "dated 6-8-77). p22 continues the narrative without a separate "
        "FD-350 mount sheet."
    ),
}

PLACEHOLDER_TEXT = "[WITHHELD - FOIA 4-750]"


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


def reclassify(pages: list[dict], docs: list[dict]) -> int:
    """Update kind on docs.jsonl and doc_kind on member pages. Return count of changes."""
    changed = 0
    docs_by_id = {d["doc_id"]: d for d in docs}
    for doc_id, new_kind in RECLASSIFY.items():
        d = docs_by_id.get(doc_id)
        if not d:
            print(f"  WARN {doc_id} not in docs.jsonl, skipping")
            continue
        if d["kind"] != new_kind:
            d["kind"] = new_kind
            changed += 1
        # Propagate to pages
        for p in pages:
            if p.get("doc_id") == doc_id and p.get("doc_kind") != new_kind:
                p["doc_kind"] = new_kind
    return changed


def apply_merge(pages: list[dict], docs: list[dict], spec: dict) -> bool:
    """Same merge mechanics used by the Denver teletype + Grand Junction passes."""
    parent_id = spec["parent"]
    absorbed = set(spec["absorbed"])
    placeholders = spec["placeholders"]

    parent_entry = next((d for d in docs if d["doc_id"] == parent_id), None)
    if parent_entry is None:
        print(f"  SKIP {parent_id}: parent not in docs.jsonl")
        return False

    absorbed_page_nos = sorted(
        p["page_no"] for p in pages if p.get("doc_id") in absorbed
    )

    if (
        len(parent_entry["page_nos"]) == len(absorbed_page_nos)
        and parent_entry["page_nos"] == absorbed_page_nos
    ):
        print(f"  SKIP {parent_id}: already merged to {len(absorbed_page_nos)} pages")
        return False

    new_count = len(absorbed_page_nos)
    for idx, pn in enumerate(absorbed_page_nos, start=1):
        page = next(p for p in pages if p["page_no"] == pn)
        page["doc_id"] = parent_id
        page["doc_kind"] = spec["kind"]
        page["doc_page_index"] = idx
        page["doc_page_count"] = new_count
        if pn in placeholders:
            page["inline_placeholder"] = PLACEHOLDER_TEXT
        else:
            page.pop("inline_placeholder", None)

    case_nums: set[str] = set()
    for d in docs:
        if d["doc_id"] in absorbed:
            case_nums.update(d.get("case_nums", []))

    merged = {
        "doc_id": parent_id,
        "kind": spec["kind"],
        "page_nos": absorbed_page_nos,
        "page_count": new_count,
        "first_template": spec["first_template"],
        "case_nums": sorted(case_nums),
        "note": spec["note"],
    }
    if placeholders:
        merged["withheld_page_nos"] = sorted(placeholders)

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
    print(f"    pages: {absorbed_page_nos}")
    print(f"    removed: {', '.join(removed)}")
    return True


def main() -> None:
    pages, docs = load()

    print("Step 1 — reclassify loose docs")
    n_reclassified = reclassify(pages, docs)
    print(f"  {n_reclassified} doc kinds updated\n")

    print(f"Step 2 — merge {MERGE['parent']}")
    merged = apply_merge(pages, docs, MERGE)
    print()

    if not n_reclassified and not merged:
        print("No changes. Files left untouched.")
        return

    save(pages, docs)
    print(f"Wrote {JSONL.name} and {DOCS.name}")
    print(f"docs.jsonl now contains {len(docs)} documents")


if __name__ == "__main__":
    main()
