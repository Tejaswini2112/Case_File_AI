"""
Step 4b — Apply human judgements about individual documents.

Grouping is rules, and rules only go so far on scanned 1970s paper. Some
documents need a person: a form whose header the detector missed, an airtel
that should count as a teletype, two pages that are obviously one newspaper
article to anyone who reads them and carry no machine-readable link.

Those decisions used to live in one-off scripts under scripts/ with document
ids hardcoded, run by hand between grouping and chunking. That failed in the
way such things do — re-running the pipeline discarded them silently, and the
corpus quietly changed underneath. They also could not be applied to a second
case, since each script encoded fixes for particular Bundy pages.

Here they are data instead: corrections/<case>.json, tracked in git, applied
by the pipeline every run. Two kinds, deliberately few:

    reclassify   this document is really a form / legal / teletype / ...
    merge        these documents are really one document

Every entry carries a `why`. In a year that sentence is the only way to tell a
considered correction from a mistake someone made once.

Runs after group_documents and before chunk_documents, on the same pages.jsonl
and docs.jsonl the grouper wrote.

Usage (run from project root):
    python -m src.ingestion.apply_corrections \\
        data/cases/bundy/ocr/bundy-part-01/pages.jsonl --case bundy
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src import paths
from src.cases import validate as validate_case

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")


def load_corrections(case: str) -> dict:
    """Read the case's corrections, or an empty set if it has none.

    A missing file is normal — a case with no corrections is the goal, not an
    error. A malformed one is not: it means someone edited it and made a
    mistake, and silently ingesting without their corrections would produce a
    corpus that looks fine and is not what they asked for.
    """
    path = paths.corrections_path(case)
    if not path.exists():
        return {"reclassify": [], "merge": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"{path} is not valid JSON: {exc}")
    data.setdefault("reclassify", [])
    data.setdefault("merge", [])
    return data


def apply_reclassify(
    pages: list[dict], docs: list[dict], entries: list[dict]
) -> tuple[int, list[str]]:
    """Set a document's kind to what a person determined it to be."""
    by_id = {d["doc_id"]: d for d in docs}
    applied = 0
    unmatched = []

    for entry in entries:
        doc_id, kind = entry["doc_id"], entry["kind"]
        doc = by_id.get(doc_id)
        if doc is None:
            # The document this correction names no longer exists. Reported
            # rather than ignored: it usually means grouping changed and the
            # correction now describes something that is not there, which is
            # exactly when a human needs to look again.
            unmatched.append(f"reclassify {doc_id} (no such document)")
            continue
        if doc["kind"] == kind:
            continue
        doc["kind"] = kind
        for page in pages:
            if page.get("doc_id") == doc_id:
                page["doc_kind"] = kind
        applied += 1

    return applied, unmatched


def apply_merge(
    pages: list[dict], docs: list[dict], entries: list[dict]
) -> tuple[int, list[str]]:
    """Fold documents into the one they belong to.

    The absorbed documents are removed rather than renumbered, matching what
    the grouper's own absorption pass does: document ids appear in citations
    and in the index's vector ids, so renumbering silently repoints every
    reference.
    """
    applied = 0
    unmatched = []

    for entry in entries:
        by_id = {d["doc_id"]: d for d in docs}
        parent = by_id.get(entry["parent"])
        if parent is None:
            unmatched.append(f"merge into {entry['parent']} (no such document)")
            continue

        absorbed = [by_id[a] for a in entry["absorb"] if a in by_id]
        missing = [a for a in entry["absorb"] if a not in by_id]
        if missing:
            unmatched.append(f"merge absorbing {', '.join(missing)} (no such document)")
        if not absorbed:
            continue

        for doc in absorbed:
            parent["page_nos"].extend(doc["page_nos"])
            parent["case_nums"] = sorted(set(parent["case_nums"]) | set(doc["case_nums"]))
            docs.remove(doc)
        parent["page_nos"].sort()
        parent["page_count"] = len(parent["page_nos"])
        if entry.get("kind"):
            parent["kind"] = entry["kind"]
        applied += 1

    return applied, unmatched


def reassign_pages(pages: list[dict], docs: list[dict]) -> None:
    """Rebuild every page's document fields from the documents.

    Derived from one source rather than patched in three places, so doc_id,
    position and kind cannot end up disagreeing with each other.
    """
    by_id = {d["doc_id"]: d for d in docs}
    page_to_doc = {pn: d["doc_id"] for d in docs for pn in d["page_nos"]}

    for page in pages:
        doc_id = page_to_doc.get(page["page_no"])
        if not doc_id:
            continue
        doc = by_id[doc_id]
        page["doc_id"] = doc_id
        page["doc_page_index"] = doc["page_nos"].index(page["page_no"]) + 1
        page["doc_kind"] = doc["kind"]
        page["doc_page_count"] = len(doc["page_nos"])


def main() -> None:
    ap = argparse.ArgumentParser(description="Apply a case's document corrections.")
    ap.add_argument("jsonl_path", type=Path, help="pages.jsonl from group_documents.py")
    ap.add_argument("--case", required=True, help="Case these documents belong to")
    args = ap.parse_args()

    try:
        case = validate_case(args.case)
    except ValueError as exc:
        sys.exit(str(exc))

    if not args.jsonl_path.exists():
        sys.exit(f"Not found: {args.jsonl_path}")
    docs_path = args.jsonl_path.parent / "docs.jsonl"
    if not docs_path.exists():
        sys.exit(f"Not found: {docs_path}. Run group_documents.py first.")

    corrections = load_corrections(case)
    stem = args.jsonl_path.parent.name

    pages = [json.loads(x) for x in args.jsonl_path.open(encoding="utf-8") if x.strip()]
    docs = [json.loads(x) for x in docs_path.open(encoding="utf-8") if x.strip()]

    # Only this document's corrections. One corrections file covers a whole
    # case, while the pipeline runs on one source PDF at a time.
    def for_this_source(entries: list[dict], key: str) -> list[dict]:
        return [e for e in entries if e[key].startswith(f"{stem}__")]

    reclass = for_this_source(corrections["reclassify"], "doc_id")
    merges = for_this_source(corrections["merge"], "parent")

    if not reclass and not merges:
        print(f"No corrections for {stem}.")
        return

    # Merges first: a merge can change which documents exist, and a reclassify
    # naming an absorbed document should be reported as unmatched rather than
    # applied to something that is about to disappear.
    merged, unmatched_m = apply_merge(pages, docs, merges)
    reclassified, unmatched_r = apply_reclassify(pages, docs, reclass)
    reassign_pages(pages, docs)

    with args.jsonl_path.open("w", encoding="utf-8") as f:
        for page in pages:
            f.write(json.dumps(page, ensure_ascii=False) + "\n")
    with docs_path.open("w", encoding="utf-8") as f:
        for doc in docs:
            f.write(json.dumps(doc, ensure_ascii=False) + "\n")

    print(f"Applied to {stem}: {merged} merge(s), {reclassified} reclassification(s)")
    for problem in unmatched_m + unmatched_r:
        print(f"  UNMATCHED: {problem}")
    if unmatched_m or unmatched_r:
        print(
            "\n  A correction naming a document that does not exist usually means\n"
            "  grouping changed. Check whether it is still needed before removing it."
        )


if __name__ == "__main__":
    main()
