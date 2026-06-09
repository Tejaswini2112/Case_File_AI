"""
Step 5 — Chunking pass.

Walks pages in document order (grouped by doc_id from step 4), concatenates each
doc's clean_text, and splits into ~500-token chunks that:
  - never cross doc_id boundaries (so a teletype's body never bleeds into the
    next newspaper clipping)
  - prefer page boundaries first, then sentence boundaries, then word
    boundaries as a fallback
  - overlap by ~100 tokens so an answer that straddles a cut still gets
    retrieved by either neighbor

The output is one JSON object per chunk, ready for embedding + Pinecone upsert.

A chunk carries enough metadata to (a) pre-filter by case/doc_kind at retrieval
time and (b) render a real citation back to a page range:

    {
      "chunk_id":       "bundy-part-01__doc-017__chunk-02",
      "doc_id":         "bundy-part-01__doc-017",
      "source_stem":    "bundy-part-01",
      "doc_kind":       "teletype",
      "doc_template":   "FD-36",
      "page_nos":       [27, 28],          # pages this chunk's text spans
      "case_nums":      ["886895", "8810975"],
      "chunk_index":    2,
      "chunk_count":    5,
      "token_estimate": 487,
      "char_count":     1830,
      "text":           "[p.27] ON JUNE 9, 1977 ..."
    }

Special cases:
  - deletion-sheet docs (4-750 FOIA placeholders) have empty clean_text. We
    synthesize a tiny "[FOIA withholding: ...]" chunk from the extracted
    metadata so 'what was withheld?' stays answerable.
  - skipped/empty pages get no doc_id from step 4 and are naturally ignored.

Usage (run from project root):
    python src/chunk_documents.py data/ocr/bundy-part-01/pages.jsonl
    python src/chunk_documents.py data/ocr/bundy-part-01/pages.jsonl --dry-run
"""

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

TARGET_TOKENS = 500
# A doc that fits in this many tokens stays as one chunk — no point splitting
# a 580-token doc into two awkward halves just to honor the 500 target.
MAX_WHOLE_DOC_TOKENS = 600
OVERLAP_TOKENS = 100

# ---------------------------------------------------------------------------
# Token estimator
# ---------------------------------------------------------------------------

# Word-count proxy. For English prose 1 word averages ~1.3 tokens; this is
# within ~15% of true tokenizer counts — good enough to size 500-token chunks.
# The embedding model retokenizes its own way at upsert time, so exact parity
# here buys us nothing and dodges the tiktoken first-run download hassle on
# Windows Python.
_WORDS_PER_TOKEN = 1 / 1.3


def count_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, int(len(text.split()) / _WORDS_PER_TOKEN))


# ---------------------------------------------------------------------------
# Sentence splitting (with abbreviation guard)
# ---------------------------------------------------------------------------

# A boundary is a .!? followed by whitespace and a capital-letter-or-bracket
# (so we also catch sentences that start with "[REDACTED]" or "[p.N]").
_SENT_BOUNDARY_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\[])")

# Tokens whose trailing period is almost never a sentence end. Kept small on
# purpose — false negatives (missed boundary) cost less than false positives
# (a wrong boundary truncates a sentence). Single capital letters (middle
# initials like "W." in "JAMES W. MC CONKIE") are handled separately.
_ABBREVIATIONS = {
    "Mr", "Mrs", "Ms", "Dr", "St", "Jr", "Sr",
    "AUSA", "SA", "SAC", "USAO",
    "Inc", "Co", "Ltd", "Corp",
    "No", "vs", "v",
    "Sgt", "Lt", "Capt", "Col", "Gen", "Maj",
}


def split_sentences(text: str) -> list[str]:
    """
    Split a single line of cleaned OCR text into sentences.

    Boundaries are punctuation-based (the cleaner collapsed newlines, so we
    can't use them). The abbreviation guard prevents false cuts after titles
    ("Mr.") or middle initials ("W.").
    """
    if not text.strip():
        return []

    cuts = [0]
    for m in _SENT_BOUNDARY_RE.finditer(text):
        period_pos = m.start() - 1
        # Walk back to find the start of the token immediately before the period.
        k = period_pos
        while k > 0 and text[k - 1] not in " \t\n":
            k -= 1
        token_before = text[k:period_pos]

        if token_before in _ABBREVIATIONS:
            continue
        if len(token_before) == 1 and token_before.isupper():
            continue
        cuts.append(m.end())
    cuts.append(len(text))

    out: list[str] = []
    for i in range(len(cuts) - 1):
        s = text[cuts[i] : cuts[i + 1]].strip()
        if s:
            out.append(s)
    return out


# ---------------------------------------------------------------------------
# Doc → (sentence, page_no) stream
# ---------------------------------------------------------------------------


def build_doc_sentences(pages: list[dict]) -> list[tuple[str, int]]:
    """
    Flatten a doc's pages into (sentence, page_no) tuples in reading order.

    We prepend a "[p.N]" marker to the first sentence of each page so the
    marker travels with the sentence into whichever chunk consumes it —
    useful when the LLM later reads the chunk and wants to attribute a fact
    to a specific page.
    """
    out: list[tuple[str, int]] = []
    for p in pages:
        text = (p.get("clean_text") or "").strip()
        if not text:
            continue
        sents = split_sentences(text)
        if not sents:
            continue
        sents[0] = f"[p.{p['page_no']}] {sents[0]}"
        for s in sents:
            out.append((s, p["page_no"]))
    return out


# ---------------------------------------------------------------------------
# Chunker
# ---------------------------------------------------------------------------


def _pack_chunks(
    sent_pages: list[tuple[str, int]],
    target: int,
    overlap: int,
) -> list[dict]:
    """Sliding window over sentences with token-budgeted packing + overlap."""
    n = len(sent_pages)
    tokens_per = [count_tokens(s) for s, _ in sent_pages]

    chunks: list[dict] = []
    i = 0
    while i < n:
        j = i
        running = 0
        while j < n and running + tokens_per[j] <= target:
            running += tokens_per[j]
            j += 1

        # A single sentence longer than target — force it through alone.
        if j == i:
            j = i + 1
            running = tokens_per[i]

        slice_pairs = sent_pages[i:j]
        chunks.append({
            "text": " ".join(s for s, _ in slice_pairs),
            "page_nos": sorted({p for _, p in slice_pairs}),
            "token_estimate": running,
        })

        if j >= n:
            break

        # Walk back from j to find the overlap start, then force progress
        # past i so we don't loop forever on degenerate input.
        overlap_acc = 0
        new_i = j
        while new_i > i and overlap_acc < overlap:
            new_i -= 1
            overlap_acc += tokens_per[new_i]
        if new_i <= i:
            new_i = i + 1
        i = new_i

    return chunks


def chunk_doc(sent_pages: list[tuple[str, int]]) -> list[dict]:
    if not sent_pages:
        return []
    total = sum(count_tokens(s) for s, _ in sent_pages)
    if total <= MAX_WHOLE_DOC_TOKENS:
        return [{
            "text": " ".join(s for s, _ in sent_pages),
            "page_nos": sorted({p for _, p in sent_pages}),
            "token_estimate": total,
        }]
    return _pack_chunks(sent_pages, TARGET_TOKENS, OVERLAP_TOKENS)


# ---------------------------------------------------------------------------
# Deletion-sheet special case
# ---------------------------------------------------------------------------


def synthesize_deletion_chunk(pages: list[dict]) -> str:
    """
    Build a tiny searchable string for a FOIA 4-750 deletion placeholder.
    Carries the deletion reference and cited exemptions so 'what was withheld
    here, and under which exemption?' still has something to retrieve.
    """
    refs: list[str] = []
    exemptions: set[str] = set()
    for p in pages:
        meta = p.get("extracted_metadata") or {}
        if meta.get("deletion_reference"):
            refs.append(meta["deletion_reference"])
        for e in meta.get("exemptions_cited") or []:
            exemptions.add(e)

    parts = ["[FOIA withholding"]
    parts.append(f"pages={','.join(str(p['page_no']) for p in pages)}")
    if refs:
        parts.append(f"reference={'; '.join(refs)}")
    if exemptions:
        parts.append(f"exemptions={','.join(sorted(exemptions))}")
    return "; ".join(parts) + "]"


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def load_doc_summaries(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    out: dict[str, dict] = {}
    for line in path.open(encoding="utf-8"):
        if line.strip():
            d = json.loads(line)
            out[d["doc_id"]] = d
    return out


def group_pages_by_doc(pages: list[dict]) -> tuple[list[str], dict[str, list[dict]]]:
    docs: dict[str, list[dict]] = defaultdict(list)
    order: list[str] = []
    for p in pages:
        doc_id = p.get("doc_id")
        if not doc_id:
            continue
        if doc_id not in docs:
            order.append(doc_id)
        docs[doc_id].append(p)
    return order, docs


def build_all_chunks(
    pages: list[dict],
    source_stem: str,
    doc_summaries: dict[str, dict],
) -> list[dict]:
    order, docs = group_pages_by_doc(pages)
    all_chunks: list[dict] = []

    for doc_id in order:
        doc_pages = docs[doc_id]
        kind = doc_pages[0].get("doc_kind") or "unknown"
        first_template = doc_pages[0].get("template")
        case_nums = doc_summaries.get(doc_id, {}).get("case_nums", [])

        if kind == "deletion-sheet":
            text = synthesize_deletion_chunk(doc_pages)
            raw_chunks = [{
                "text": text,
                "page_nos": [p["page_no"] for p in doc_pages],
                "token_estimate": count_tokens(text),
            }]
        else:
            sent_pages = build_doc_sentences(doc_pages)
            raw_chunks = chunk_doc(sent_pages)

        if not raw_chunks:
            continue

        for idx, c in enumerate(raw_chunks, 1):
            all_chunks.append({
                "chunk_id": f"{doc_id}__chunk-{idx:02d}",
                "doc_id": doc_id,
                "source_stem": source_stem,
                "doc_kind": kind,
                "doc_template": first_template,
                "page_nos": c["page_nos"],
                "case_nums": case_nums,
                "chunk_index": idx,
                "chunk_count": len(raw_chunks),
                "token_estimate": c["token_estimate"],
                "char_count": len(c["text"]),
                "text": c["text"],
            })

    return all_chunks


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def print_summary(chunks: list[dict]) -> None:
    if not chunks:
        print("No chunks produced.")
        return

    doc_ids = {c["doc_id"] for c in chunks}
    print(f"\nGenerated {len(chunks)} chunks from {len(doc_ids)} documents.\n")

    by_kind = Counter(c["doc_kind"] for c in chunks)
    print("By doc_kind:")
    for k, n in by_kind.most_common():
        print(f"  {k:<16} {n:>3}")

    tokens = sorted(c["token_estimate"] for c in chunks)
    print("\nToken stats:")
    print(f"  min:    {tokens[0]}")
    print(f"  max:    {tokens[-1]}")
    print(f"  mean:   {sum(tokens) // len(tokens)}")
    print(f"  median: {tokens[len(tokens) // 2]}")

    by_doc = Counter(c["doc_id"] for c in chunks)
    multi = [(d, n) for d, n in by_doc.items() if n > 1]
    if multi:
        print(f"\nMulti-chunk documents ({len(multi)}):")
        for d, n in sorted(multi):
            pages = sorted({pn for c in chunks if c["doc_id"] == d for pn in c["page_nos"]})
            print(f"  {d}  {n} chunks  pages={pages}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("jsonl_path", type=Path,
                    help="Path to pages.jsonl produced by group_documents.py")
    ap.add_argument("--dry-run", action="store_true",
                    help="Compute chunks and print summary, but don't write chunks.jsonl")
    args = ap.parse_args()

    if not args.jsonl_path.exists():
        sys.exit(f"Not found: {args.jsonl_path}")

    pages = [json.loads(line) for line in args.jsonl_path.open(encoding="utf-8") if line.strip()]
    if not any(p.get("doc_id") for p in pages):
        sys.exit(f"No doc_id fields in {args.jsonl_path}. "
                 f"Run: python src/group_documents.py {args.jsonl_path}")

    source_stem = args.jsonl_path.parent.name
    doc_summaries = load_doc_summaries(args.jsonl_path.parent / "docs.jsonl")

    chunks = build_all_chunks(pages, source_stem, doc_summaries)
    print_summary(chunks)

    if args.dry_run:
        print("\n(dry-run: chunks.jsonl not written)")
        return

    out_path = args.jsonl_path.parent / "chunks.jsonl"
    with out_path.open("w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    print(f"\nWrote {len(chunks)} chunks to {out_path}")


if __name__ == "__main__":
    main()
