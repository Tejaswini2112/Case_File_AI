"""
One-off chunk quality audit. Run from project root:
    python inspect_chunks.py data/ocr/bundy-part-01/chunks.jsonl

Reports:
  - field completeness
  - token-size distribution
  - text-noise diagnostics (redaction density, non-letter ratio, suspect chunks)
  - metadata sanity (doc_kind enum, page_nos integrity, case_nums format)
  - coverage gaps (pages in clean/ but not in any chunk)
"""

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

if len(sys.argv) < 2:
    sys.exit("Usage: python inspect_chunks.py data/ocr/<name>/chunks.jsonl")

chunks_path = Path(sys.argv[1])
pages_path = chunks_path.parent / "pages.jsonl"

chunks = [json.loads(l) for l in chunks_path.open(encoding="utf-8") if l.strip()]
pages = [json.loads(l) for l in pages_path.open(encoding="utf-8") if l.strip()]

print("=" * 70)
print(f"AUDIT — {chunks_path}")
print("=" * 70)

# ---------------------------------------------------------------------------
# 1. Required fields
# ---------------------------------------------------------------------------
REQUIRED = ["chunk_id","doc_id","source_stem","doc_kind","doc_template",
            "page_nos","case_nums","chunk_index","chunk_count",
            "token_estimate","char_count","text"]

print(f"\n[1] FIELD COMPLETENESS  (n={len(chunks)})")
missing = defaultdict(list)
for c in chunks:
    for f in REQUIRED:
        if f not in c or c[f] is None:
            missing[f].append(c["chunk_id"])
        elif f == "text" and not str(c[f]).strip():
            missing[f].append(c["chunk_id"])
if missing:
    for f, ids in missing.items():
        print(f"  MISSING {f}: {len(ids)} chunks  e.g. {ids[:3]}")
else:
    print("  OK — all chunks have all required fields populated.")

# ---------------------------------------------------------------------------
# 2. Uniqueness + index integrity
# ---------------------------------------------------------------------------
print(f"\n[2] ID + INDEX INTEGRITY")
ids = [c["chunk_id"] for c in chunks]
if len(ids) != len(set(ids)):
    print(f"  DUPLICATE chunk_ids: {len(ids)-len(set(ids))}")
else:
    print(f"  OK — all {len(ids)} chunk_ids unique.")

per_doc = defaultdict(list)
for c in chunks:
    per_doc[c["doc_id"]].append(c)
idx_errs = []
for doc_id, cs in per_doc.items():
    cs_sorted = sorted(cs, key=lambda c: c["chunk_index"])
    expected_count = cs[0]["chunk_count"]
    if any(c["chunk_count"] != expected_count for c in cs):
        idx_errs.append(f"{doc_id}: inconsistent chunk_count")
    if [c["chunk_index"] for c in cs_sorted] != list(range(1, len(cs)+1)):
        idx_errs.append(f"{doc_id}: chunk_index not contiguous 1..N")
    if expected_count != len(cs):
        idx_errs.append(f"{doc_id}: chunk_count={expected_count} but found {len(cs)}")
if idx_errs:
    for e in idx_errs:
        print(f"  {e}")
else:
    print(f"  OK — chunk_index/chunk_count consistent across {len(per_doc)} docs.")

# ---------------------------------------------------------------------------
# 3. Token-size distribution
# ---------------------------------------------------------------------------
print(f"\n[3] TOKEN-SIZE DISTRIBUTION")
tokens = sorted(c["token_estimate"] for c in chunks)
print(f"  min={tokens[0]}  p25={tokens[len(tokens)//4]}  median={tokens[len(tokens)//2]}  "
      f"p75={tokens[3*len(tokens)//4]}  max={tokens[-1]}")
buckets = [(0,30,"< 30"),(30,100,"30-100"),(100,300,"100-300"),
           (300,500,"300-500"),(500,600,"500-600"),(600,10000,"> 600")]
for lo, hi, label in buckets:
    n = sum(1 for t in tokens if lo <= t < hi)
    bar = "#" * n
    print(f"  {label:>9} : {n:>3} {bar}")

# Tiny chunks — list them
tiny = sorted([c for c in chunks if c["token_estimate"] < 30],
              key=lambda c: c["token_estimate"])
if tiny:
    print(f"\n  TINY CHUNKS (< 30 tokens) — {len(tiny)} total:")
    for c in tiny:
        print(f"    {c['chunk_id']:<48} {c['doc_kind']:<16} tok={c['token_estimate']}  text={c['text'][:90]!r}")

# ---------------------------------------------------------------------------
# 4. Noise diagnostics
# ---------------------------------------------------------------------------
print(f"\n[4] TEXT NOISE DIAGNOSTICS")

def letter_ratio(s):
    if not s: return 0.0
    return sum(c.isalpha() and c.isascii() for c in s) / len(s)

def redaction_count(s):
    return len(re.findall(r"\[REDACTED\]", s))

def page_marker_count(s):
    return len(re.findall(r"\[p\.\d+\]", s))

# Noise score = letter ratio (high = clean prose, low = junk)
noisy = []
for c in chunks:
    # Strip the [REDACTED] and [p.N] markers before measuring noise — those
    # are intentional structural inserts, not OCR garbage.
    body = re.sub(r"\[REDACTED\]|\[p\.\d+\]", "", c["text"])
    lr = letter_ratio(body)
    if lr < 0.50:
        noisy.append((lr, c))

print(f"  Chunks with letter-ratio < 50% (likely junk-heavy): {len(noisy)}")
for lr, c in sorted(noisy)[:10]:
    print(f"    {c['chunk_id']:<48} {c['doc_kind']:<16} letter_ratio={lr:.2f}")

# Redaction density distribution
red_per_chunk = [redaction_count(c["text"]) for c in chunks]
print(f"\n  Redactions per chunk: min={min(red_per_chunk)}, max={max(red_per_chunk)}, "
      f"total={sum(red_per_chunk)}")
high_red = [c for c, n in zip(chunks, red_per_chunk) if n >= 10]
if high_red:
    print(f"  Chunks with >=10 [REDACTED] markers: {len(high_red)}")
    for c in high_red[:5]:
        n = redaction_count(c["text"])
        print(f"    {c['chunk_id']:<48} {c['doc_kind']:<16} redactions={n}  tokens={c['token_estimate']}")

# Find chunks where redactions outnumber 1/5 of token count (signal-to-noise weak)
print(f"\n  Chunks where redactions are > 20% of token count:")
flagged = []
for c, n in zip(chunks, red_per_chunk):
    if c["token_estimate"] > 0 and n / c["token_estimate"] > 0.20:
        flagged.append((n / c["token_estimate"], c, n))
for ratio, c, n in sorted(flagged, reverse=True)[:8]:
    print(f"    {c['chunk_id']:<48} red={n} tok={c['token_estimate']} ratio={ratio:.2f}")
if not flagged:
    print("    (none)")

# Detect non-ASCII / Unicode oddities
weird = []
for c in chunks:
    non_ascii = sum(1 for ch in c["text"] if ord(ch) > 127)
    if non_ascii > 20:
        weird.append((non_ascii, c))
if weird:
    print(f"\n  Chunks with >20 non-ASCII chars (smart quotes, em-dashes, OCR garble):")
    for n, c in sorted(weird, reverse=True)[:5]:
        sample = "".join(ch for ch in c["text"] if ord(ch) > 127)[:40]
        print(f"    {c['chunk_id']:<48} non_ascii={n}  sample={sample!r}")

# ---------------------------------------------------------------------------
# 5. Metadata sanity
# ---------------------------------------------------------------------------
print(f"\n[5] METADATA SANITY")

kind_counts = Counter(c["doc_kind"] for c in chunks)
print(f"  doc_kind values: {dict(kind_counts)}")
tmpl_counts = Counter(c["doc_template"] for c in chunks)
print(f"  doc_template values: {dict(tmpl_counts)}")
source_counts = Counter(c["source_stem"] for c in chunks)
print(f"  source_stem values: {dict(source_counts)}")

# page_nos must be list[int], non-empty, sorted, no duplicates
bad_pages = []
for c in chunks:
    p = c["page_nos"]
    if not isinstance(p, list) or not p:
        bad_pages.append((c["chunk_id"], "empty/not-list"))
    elif not all(isinstance(x, int) for x in p):
        bad_pages.append((c["chunk_id"], "non-int element"))
    elif p != sorted(set(p)):
        bad_pages.append((c["chunk_id"], "unsorted or duplicates"))
print(f"  page_nos malformed: {len(bad_pages)}")
for cid, reason in bad_pages[:5]:
    print(f"    {cid}: {reason}")

# case_nums format — all digits, normalized
bad_cn = []
case_num_format_examples = set()
for c in chunks:
    for cn in c.get("case_nums", []):
        case_num_format_examples.add(cn)
        if not cn.isdigit():
            bad_cn.append((c["chunk_id"], cn))
print(f"  case_nums seen (sample): {sorted(case_num_format_examples)[:10]}")
if bad_cn:
    print(f"  case_nums with non-digit chars: {len(bad_cn)}  e.g. {bad_cn[:3]}")
else:
    print(f"  case_nums all normalized (digits only) — Pinecone filter-safe.")

# Pinecone metadata limits: total metadata <= 40 KB per record
oversized = []
for c in chunks:
    meta = {k: c[k] for k in ("doc_id","source_stem","doc_kind","doc_template",
                              "page_nos","case_nums","chunk_index","chunk_count","text")}
    size = len(json.dumps(meta, ensure_ascii=False).encode("utf-8"))
    if size > 40_000:
        oversized.append((c["chunk_id"], size))
print(f"  Chunks exceeding Pinecone's 40KB metadata limit: {len(oversized)}")
for cid, sz in oversized[:5]:
    print(f"    {cid}: {sz} bytes")

# ---------------------------------------------------------------------------
# 6. Coverage gap
# ---------------------------------------------------------------------------
print(f"\n[6] COVERAGE GAP")
pages_with_text = {p["page_no"] for p in pages if (p.get("clean_text") or "").strip()}
pages_with_deletion = {p["page_no"] for p in pages if p.get("doc_kind") == "deletion-sheet" or p.get("template") == "4-750"}
pages_in_chunks = set()
for c in chunks:
    pages_in_chunks.update(c["page_nos"])

uncovered_text = pages_with_text - pages_in_chunks
print(f"  Pages with clean_text:       {len(pages_with_text)}")
print(f"  Pages referenced by chunks:  {len(pages_in_chunks)}")
print(f"  Pages with text but NOT in any chunk: {len(uncovered_text)}")
if uncovered_text:
    print(f"    {sorted(uncovered_text)}")

# Pages with deletion-sheets but uncovered would be a separate concern
unhandled_deletions = pages_with_deletion - pages_in_chunks
if unhandled_deletions:
    print(f"  Deletion-sheet pages not in any chunk: {sorted(unhandled_deletions)}")

print("\n" + "=" * 70)
print("END OF AUDIT")
print("=" * 70)
