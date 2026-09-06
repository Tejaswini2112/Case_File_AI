"""
Step 4 — Document grouping pass.

A single FBI document often spans multiple physical PDF pages: a teletype
runs FD-36 + FD-36-cont + FD-36-cont, a newspaper clipping carries a
"Continued from A-1" jump, the FOIA cover sheet is 3 pages, etc. For RAG
we want to retrieve at the document level, not the page level — otherwise
half a teletype gets surfaced without the warning paragraph from page 2.

This pass walks pages in order and assigns each page to a doc_id. It does
NOT alter raw_text or clean_text; it only adds three fields:

    doc_id           e.g. "bundy-part-01__doc-007"
    doc_page_index   1-based position of this page within the doc
    doc_kind         one of:  cover, teletype, teletype-cont, newspaper,
                              deletion-sheet, form, loose

The grouping rules (in order, first match wins):

  1. Skipped / empty pages get no doc assignment.
  2. 4-750 deletion sheets start their own document during the linear pass.
     Whether they KEEP it is decided afterwards — see below.
  3. Strong "continues previous doc" signals:
        - template == FD-36-cont
        - first ~12 lines contain "PAGE TWO/THREE/.../FIFTEEN"
        - body contains "Continued from"
     If the current doc is compatible (teletype/newspaper/loose), continue it.
     Otherwise start a new doc and mark it as orphan-continuation.
  4. A page with a detected form template (FD-36, FD-350, FD-xxx, etc.)
     starts a new doc.
  5. Unknown-template pages: weak heuristic. If the previous page ended
     mid-sentence and at least one case-file number is shared, continue
     the current doc. Otherwise start a new "loose" doc.
  6. The FOIA cover preamble (page 1-3 of bundy-part-01) is grouped as
     one doc by a special rule: if page_no <= 3 AND page mentions
     "FREEDOM OF INFORMATION", "COVER SHEET", or "THE BEST COPY".

Then one pass that cannot be done linearly:

  7. A withholding that INTERRUPTS a document is folded back into it, along
     with the pages that follow it. Whether a 4-750 ends a document or
     interrupts one is only answerable by looking at what comes after it, and
     the linear pass has not read that yet. Left unfixed it splits one
     teletype into three: body, withholding, and a remainder that then looks
     like an orphan because the document it would continue has become a
     deletion sheet — one wrong rule producing two symptoms.

     This was previously corrected by hand, by one-off scripts under scripts/
     with document ids hardcoded, which re-running the pipeline silently
     discarded.

Usage (run from project root):
    python src/group_documents.py data/cases/bundy/ocr/bundy-part-01/pages.jsonl
    python src/group_documents.py data/cases/bundy/ocr/bundy-part-01/pages.jsonl --dry-run
"""

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

PAGE_N_RE = re.compile(
    r"\bPAGE\s+(?:TWO|THREE|FOUR|FIVE|SIX|SEVEN|EIGHT|NINE|TEN|"
    r"ELEVEN|TWELVE|THIRTEEN|FOURTEEN|FIFTEEN|SIXTEEN)\b",
    re.IGNORECASE,
)
CONT_FROM_RE = re.compile(r"continued\s+from\b", re.IGNORECASE)
SEE_MORE_RE = re.compile(r"\bSee\s+BUN[DT]Y\s+on\b", re.IGNORECASE)
CASE_NUM_RE = re.compile(r"\b88[-=~]?\s*\d{4,6}\b")
COVER_MARKERS_RE = re.compile(
    r"FREEDOM\s+OF\s+INFORMATION|COVER\s+SHEET|THE\s+BEST\s+COPY",
    re.IGNORECASE,
)
# Anchored form-ID line: matches "FD-36 (Rev. 7-27-76)" / "FD-350 (Rew. 11-11-75)"
# / "4-750 (Rev. 12-14-88)" / "FD-65 (Rev. 8-5-74)" in the first few lines.
FORM_HEADER_RE = re.compile(
    r"^\s*(?:FD[-.]?\s*\d{1,3}|4[-.]?\s*750)\s*\(?(?:Rev|Mev|Rew|Hee|fev|Rey|Gev)",
    re.IGNORECASE,
)


def normalize_case_num(s: str) -> str:
    """OCR sometimes reads `-` as `=` or splits digits. Normalize for matching."""
    return re.sub(r"[^\d]", "", s)


def case_numbers(text: str) -> set[str]:
    return {normalize_case_num(m.group(0)) for m in CASE_NUM_RE.finditer(text)}


def has_page_n_header(text: str) -> bool:
    # PAGE N marker sits near the top — but on FD-36 continuations it can be
    # below ~12 lines of form furniture (FD-36 form-ID line, transmit checkboxes,
    # date line, dashed separator). Scan generously.
    head = "\n".join(text.splitlines()[:20])
    return bool(PAGE_N_RE.search(head))


def has_continued_from(text: str) -> bool:
    return bool(CONT_FROM_RE.search(text))


def has_form_header(text: str) -> bool:
    head = "\n".join(text.splitlines()[:6])
    return bool(FORM_HEADER_RE.search(head))


def is_cover_page(p: dict) -> bool:
    return p["page_no"] <= 3 and bool(COVER_MARKERS_RE.search(p.get("raw_text", "")))


def ends_mid_sentence(text: str) -> bool:
    """True if the page's last non-blank line looks unfinished."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return True
    last = lines[-1]
    # Ends with a hard punctuation mark → finished
    return not last.endswith((".", "!", "?", '"', "”", "”"))


def starts_mid_sentence(text: str) -> bool:
    """
    True if the page's first non-blank, non-noise line looks like it picks up
    mid-sentence (lowercase start, or a short fragment). Used as the second
    half of the weak unknown-template continuation rule — without this, a
    shared case# alone groups unrelated docs that just happen to belong to
    the same field office file.
    """
    for ln in text.splitlines():
        s = ln.strip()
        if not s:
            continue
        # Skip pure-symbol noise lines (~, &, _, etc.)
        if not any(c.isalpha() for c in s):
            continue
        first = s[0]
        # Lowercase start → almost certainly mid-sentence
        if first.islower():
            return True
        # 1-2 char fragments (e.g. "Bundy -", "ho") are typical of mid-narrative
        first_word = s.split()[0]
        if len(first_word) <= 2 and first_word.lower() not in {"i", "a", "fm", "to", "re", "bt", "sa"}:
            return True
        return False
    return False


# ---------------------------------------------------------------------------
# Per-page decision
# ---------------------------------------------------------------------------


def classify_doc_kind(template: str | None, text: str) -> str:
    if template == "4-750":
        return "deletion-sheet"
    if template == "FD-36":
        return "teletype"
    if template == "FD-36-cont":
        return "teletype-cont"
    if template == "FD-350":
        return "newspaper"
    if template in ("FD-xxx",):
        return "form"
    return "loose"


CONTINUATION_COMPATIBLE = {"teletype", "teletype-cont", "newspaper", "loose"}


def looks_like_continuation(page: dict) -> bool:
    """Does this page carry a signal that it continues an earlier one?

    The same three signals decide() uses, asked without reference to what came
    before. decide() can only answer "continue this document or start a new
    one", and when the document immediately before is a FOIA withholding the
    answer is forced to "new" — discarding the fact that the page said it was a
    continuation. Recording it separately keeps that evidence for the
    absorption pass, which is the only place that can act on it.
    """
    text = page.get("raw_text") or ""
    return (
        page.get("template") == "FD-36-cont"
        or has_page_n_header(text)
        or has_continued_from(text)
    )


def decide(page: dict, current: dict | None, prev_page: dict | None) -> str:
    """Return 'new' or 'continue'."""
    template = page.get("template")
    text = page.get("raw_text") or ""

    # Rule 1: deletion sheets are always their own doc
    if template == "4-750":
        return "new"

    # Rule 2: cover preamble (pages 1-3 with cover markers) groups together
    if is_cover_page(page):
        if current and current["kind"] == "cover":
            return "continue"
        return "new"

    if current is None:
        return "new"

    current_kind = current["kind"]

    # Rule 3a: explicit continuation by template
    if template == "FD-36-cont":
        if current_kind in {"teletype", "teletype-cont", "loose"}:
            return "continue"
        return "new"

    # Rule 3b: explicit continuation by PAGE TWO / PAGE THREE header
    if has_page_n_header(text):
        if current_kind in CONTINUATION_COMPATIBLE:
            # Case-number sanity check when both pages carry one
            here = case_numbers(text)
            there = current.get("case_nums", set())
            if not here or not there or here & there:
                return "continue"
        return "new"

    # Rule 3c: "Continued from" marker — usually newspaper article continuation
    if has_continued_from(text):
        if current_kind in {"newspaper", "loose"}:
            return "continue"
        return "new"

    # Rule 4: a form header line in the first few lines starts a new doc
    if has_form_header(text):
        return "new"

    # Rule 5: unknown template with weak continuation signal.
    # Requires THREE things together — case# alone is too permissive because
    # 88-6895 is the SLC field office file used across many separate docs.
    if template in (None, "unknown"):
        if (
            prev_page is not None
            and ends_mid_sentence(prev_page.get("raw_text", ""))
            and starts_mid_sentence(text)
        ):
            here = case_numbers(text)
            there = current.get("case_nums", set())
            if here and there and (here & there):
                return "continue"
        return "new"

    return "new"


# ---------------------------------------------------------------------------
# Grouping pass
# ---------------------------------------------------------------------------


# Text put in place of a page whose body the FOIA release withheld, so the
# absence is visible to a reader rather than silently missing.
WITHHELD_PLACEHOLDER = "[WITHHELD - FOIA 4-750]"

# Kinds that can legitimately span a withholding. A cover sheet or a
# deletion-sheet cannot be a parent; the rest can.
ABSORBING_KINDS = {"teletype", "newspaper", "legal", "memo", "form", "loose"}


def absorb_inline_withholdings(docs: list[dict]) -> int:
    """Fold FOIA withholdings that sit *inside* a document back into it.

    The linear pass treats every 4-750 deletion sheet as its own document,
    which is right when one ends a document and wrong when one interrupts it.
    A teletype whose middle page was withheld arrives as three documents: the
    body, the withholding, and the remainder — and the remainder then looks
    like an orphan continuation because the document it would continue is now
    a deletion sheet. One rule, two symptoms.

    Deciding which case a withholding is needs to look at what comes *after*
    it, and the linear pass cannot: at the moment it meets the sheet, the
    continuation has not been read yet. So this runs afterwards, when both
    sides are known.

    A withholding is absorbed only when the document following it shares a
    case-file number with the document before it. That shared number is the
    evidence they are one document; without it, a withholding that genuinely
    separates two unrelated documents would swallow the second one.

    Absorbed documents are removed, not renumbered. Their ids appear in
    citations and in the index's vector ids, so renumbering would silently
    repoint every reference to a different document.

    Returns how many documents were absorbed.
    """
    absorbed_total = 0
    i = 0
    while i < len(docs):
        parent = docs[i]
        if parent["kind"] not in ABSORBING_KINDS:
            i += 1
            continue

        # Keep extending this parent for as long as the pattern repeats — one
        # teletype can be interrupted several times.
        while True:
            start = i + 1
            end = start
            while end < len(docs) and docs[end]["kind"] == "deletion-sheet":
                end += 1

            no_withholding = end == start
            nothing_follows = end >= len(docs)
            if no_withholding or nothing_follows:
                break
            follower = docs[end]
            continues_parent = follower.get("starts_as_continuation", False)
            # Case numbers are the weaker evidence and are used only as a
            # fallback. They are OCR-derived, so the same file number appears
            # as 886895, 8810975, 8812975 and 8816975 across pages of one
            # document; and as decide() already notes, a shared number alone is
            # too permissive because one field-office file spans many separate
            # documents. It is trustworthy here only because the question is
            # narrow: not "are these related" but "did this withholding
            # interrupt this specific document".
            shares_case_number = bool(parent["case_nums"] & follower["case_nums"])
            if not (continues_parent or shares_case_number):
                break

            for doc in docs[start : end + 1]:
                parent["page_nos"].extend(doc["page_nos"])
                parent["case_nums"] |= doc["case_nums"]
                if doc["kind"] == "deletion-sheet":
                    parent.setdefault("withheld_pages", set()).update(doc["page_nos"])
            parent["page_nos"].sort()
            absorbed_total += end + 1 - start
            del docs[start : end + 1]

        i += 1
    return absorbed_total


def group_pages(pages: list[dict], source_stem: str) -> tuple[list[dict], list[dict]]:
    """
    Walk pages in order, assign doc_id + doc_page_index + doc_kind in place
    (on a copy). Return (updated_pages, doc_summaries).
    """
    updated = [dict(p) for p in pages]
    docs: list[dict] = []
    current: dict | None = None
    prev_assigned_page: dict | None = None

    for p in updated:
        bucket = p.get("bucket")
        text = p.get("raw_text") or ""

        # Skip pages with no usable content. Don't reset current — a 4-750
        # in the middle of a teletype shouldn't break the previous group's
        # ability to continue, but in practice 4-750s themselves become docs
        # and naturally restart current via decide().
        if bucket in ("skipped", "empty") or not text.strip():
            p["doc_id"] = None
            p["doc_page_index"] = None
            p["doc_kind"] = None
            continue

        action = decide(p, current, prev_assigned_page)

        if action == "continue" and current is not None:
            current["page_nos"].append(p["page_no"])
            current["case_nums"] |= case_numbers(text)
        else:
            kind = "cover" if is_cover_page(p) else classify_doc_kind(p.get("template"), text)
            current = {
                "doc_id": f"{source_stem}__doc-{len(docs)+1:03d}",
                "kind": kind,
                "page_nos": [p["page_no"]],
                "case_nums": case_numbers(text),
                "first_template": p.get("template"),
                # Kept for the absorption pass: a document whose first page
                # announced itself as a continuation only exists because it had
                # nothing valid to attach to.
                "starts_as_continuation": looks_like_continuation(p),
            }
            docs.append(current)

        p["doc_id"] = current["doc_id"]
        p["doc_page_index"] = len(current["page_nos"])
        p["doc_kind"] = current["kind"]
        prev_assigned_page = p

    # A withholding inside a document only becomes recognisable once the pages
    # after it have been read, so it is resolved here rather than in the loop.
    absorbed = absorb_inline_withholdings(docs)

    # Page assignments are rebuilt from the documents rather than patched,
    # because absorption changes which document a page belongs to, its position
    # within it, and its kind. Deriving all three from one source removes the
    # chance of the three disagreeing.
    by_doc = {d["doc_id"]: d for d in docs}
    page_to_doc = {
        page_no: d["doc_id"] for d in docs for page_no in d["page_nos"]
    }
    for p in updated:
        doc_id = page_to_doc.get(p["page_no"])
        if not doc_id:
            p["doc_id"] = None
            p["doc_page_index"] = None
            p["doc_kind"] = None
            p["doc_page_count"] = None
            p.pop("inline_placeholder", None)
            continue
        doc = by_doc[doc_id]
        p["doc_id"] = doc_id
        p["doc_page_index"] = doc["page_nos"].index(p["page_no"]) + 1
        p["doc_kind"] = doc["kind"]
        p["doc_page_count"] = len(doc["page_nos"])
        if p["page_no"] in doc.get("withheld_pages", set()):
            p["inline_placeholder"] = WITHHELD_PLACEHOLDER
        else:
            p.pop("inline_placeholder", None)

    if absorbed:
        print(f"  absorbed {absorbed} document(s) into the ones they interrupt")

    # Return JSON-serializable summaries (drop the `case_nums` set).
    summaries = [
        {
            "doc_id": d["doc_id"],
            "kind": d["kind"],
            "page_nos": d["page_nos"],
            "page_count": len(d["page_nos"]),
            "first_template": d["first_template"],
            "case_nums": sorted(d["case_nums"]),
        }
        for d in docs
    ]
    return updated, summaries


# ---------------------------------------------------------------------------
# I/O + CLI
# ---------------------------------------------------------------------------


def print_summary(summaries: list[dict]) -> None:
    print(f"\nGrouped into {len(summaries)} documents:\n")
    kind_counts = Counter(d["kind"] for d in summaries)
    size_buckets = Counter()
    for d in summaries:
        n = d["page_count"]
        bucket = "1 page" if n == 1 else "2 pages" if n == 2 else "3-5 pages" if n <= 5 else "6+ pages"
        size_buckets[bucket] += 1

    print("By kind:")
    for k, n in kind_counts.most_common():
        print(f"  {k:<16} {n:>3}")
    print("\nBy size:")
    for k, n in size_buckets.most_common():
        print(f"  {k:<16} {n:>3}")

    multi = [d for d in summaries if d["page_count"] > 1]
    if multi:
        print(f"\nMulti-page documents ({len(multi)}):")
        for d in multi:
            cn = ",".join(d["case_nums"][:2]) or "-"
            pgs = ",".join(str(n) for n in d["page_nos"])
            print(f"  {d['doc_id']}  {d['kind']:<14} pages=[{pgs}]  case#={cn}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("jsonl_path", type=Path)
    ap.add_argument("--dry-run", action="store_true",
                    help="Compute grouping and print summary, but don't write back.")
    args = ap.parse_args()

    if not args.jsonl_path.exists():
        sys.exit(f"Not found: {args.jsonl_path}")

    pages = [json.loads(line) for line in args.jsonl_path.open(encoding="utf-8") if line.strip()]
    source_stem = args.jsonl_path.parent.name

    updated, summaries = group_pages(pages, source_stem)
    print_summary(summaries)

    if args.dry_run:
        print("\n(dry-run: pages.jsonl not modified)")
        return

    with args.jsonl_path.open("w", encoding="utf-8") as f:
        for p in updated:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")

    # Also write a docs.jsonl summary file for downstream consumers.
    docs_path = args.jsonl_path.parent / "docs.jsonl"
    with docs_path.open("w", encoding="utf-8") as f:
        for d in summaries:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")

    print(f"\nUpdated {args.jsonl_path}")
    print(f"Wrote  {docs_path}")


if __name__ == "__main__":
    main()
