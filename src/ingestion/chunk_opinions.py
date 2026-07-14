"""
Web Step 2 (opinions) — Topic-aware chunker for court opinions.

Court opinions differ from the FBI material the main chunker (chunk_documents.py)
was built for: they are long and organized by legal issue. Cutting them every
~500 tokens (the FBI approach) would split one argument across two pieces, or
blend two arguments into one — which hurts retrieval on issue-specific questions.

So this chunker cuts along the OPINION'S OWN STRUCTURE instead of by raw size:
  - section headings (<h2>: FACTS, ISSUES ON APPEAL) — reliable, they are real tags
  - argument openings ("Next Bundy argues...", "As his first point on appeal...")
    — pattern-based, less reliable; we eyeball the result and tune the patterns.
Each topic becomes one chunk. A topic that is genuinely too long is split at
PARAGRAPH boundaries (never mid-paragraph), and only within that one topic, so
two different topics never share a chunk.

Reporter page numbers (the *334, *335 "star-pagination" markers) are tracked as
we read, so every chunk records the real So.2d pages it covers — usable for
citation, exactly like the FBI pipeline's [p.N] page numbers.

Output is chunks.jsonl in the SAME schema embed_chunks.py expects, so the
existing embed step loads it unchanged.

Usage (run from project root):
    python -m src.ingestion.chunk_opinions data/raw/opinions/bundy-1984-chi-omega.json
    python -m src.ingestion.chunk_opinions data/raw/opinions/bundy-1984-chi-omega.json --dry-run
"""

import argparse
import json
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString

# Reuse the FBI chunker's token estimator so chunk sizes are measured the same
# way across both document types (comparable stats, one source of truth).
from src.ingestion.chunk_documents import count_tokens

sys.stdout.reconfigure(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = REPO_ROOT / "data" / "web"

# A topic at or under this size stays whole; above it we split at paragraphs.
WHOLE_SECTION_MAX = 600
TARGET_TOKENS = 500
# When a topic is split, each piece carries the previous piece's last ~100 words
# as a cushion, so an answer sitting on a cut line is still caught by a neighbor.
# We measure the cushion in WORDS (not paragraphs) so it's consistent even when a
# piece is a single large paragraph — the case the paragraph-only overlap missed.
OVERLAP_WORDS = 100

# Argument-opening phrases, matched against the START of a paragraph (lowered).
# These are the "less reliable" signposts — kept in one place so tuning after
# eyeballing the output is a one-line edit.
ARG_START = re.compile(
    r"^("
    r"as (his|a|another)\b[^.]*\bpoint on appeal"          # "As his first point on appeal..."
    r"|next\b[^.]{0,50}?\b(argues|contends|questions|asserts|maintains|claims)"
    r"|appellant (also|next|further)\b"
    r"|bundy('s)? (next|also|further)\b"
    r"|[a-z]+('s)? next point on appeal"
    r")",
)


# ---------------------------------------------------------------------------
# HTML -> (text, pages), tracking star-pagination as we walk
# ---------------------------------------------------------------------------


def block_text_and_pages(block, state: dict) -> tuple[str, list[int]]:
    """
    Flatten one HTML block to text, and report which reporter pages it spans.

    `state["page"]` is the running "current reporter page". A star-pagination
    span (<span class="star-pagination">*335</span>) advances it and is itself
    dropped from the text (it is a marker, not prose).
    """
    parts: list[str] = []
    pages = {state["page"]}

    def walk(el) -> None:
        for child in el.children:
            if isinstance(child, NavigableString):
                parts.append(str(child))
            elif child.name == "span" and "star-pagination" in (child.get("class") or []):
                m = re.search(r"\d+", child.get_text())
                if m:
                    state["page"] = int(m.group())
                pages.add(state["page"])
            else:
                walk(child)

    walk(block)
    text = re.sub(r"\s+", " ", "".join(parts)).strip()
    return text, sorted(pages)


# ---------------------------------------------------------------------------
# Blocks -> topic sections
# ---------------------------------------------------------------------------


def build_sections(blocks, state: dict) -> list[dict]:
    """
    Group paragraph blocks into topic sections. A new section begins at a
    heading (<h2>) or at a paragraph that opens a new argument.
    """
    sections: list[dict] = []
    current = {"title": "(opening / caption)", "blocks": []}

    for block in blocks:
        tag = getattr(block, "name", None)
        if tag is None:
            continue
        text, pages = block_text_and_pages(block, state)
        if not text:
            continue

        is_heading = tag == "h2"
        is_arg = tag == "p" and bool(ARG_START.match(text.lower()[:90]))

        if is_heading or is_arg:
            if current["blocks"]:
                sections.append(current)
            title = text if is_heading else (text[:70] + ("..." if len(text) > 70 else ""))
            current = {"title": title, "blocks": []}
            if is_heading:
                # The heading is a label, not body prose — don't chunk it.
                continue

        current["blocks"].append((text, pages, count_tokens(text)))

    if current["blocks"]:
        sections.append(current)
    return sections


# ---------------------------------------------------------------------------
# Sections -> chunks (whole if small; paragraph-split if big)
# ---------------------------------------------------------------------------


def combine(blocks) -> dict:
    text = " ".join(b[0] for b in blocks)
    pages = sorted({p for _, ps, _ in blocks for p in ps})
    return {"text": text, "page_nos": pages, "token_estimate": count_tokens(text)}


def pack_blocks(blocks, target: int, overlap_words: int) -> list[dict]:
    """
    Greedy paragraph packer with a word-level overlap cushion.

    Primary cuts still fall at paragraph boundaries (fill to ~target tokens).
    But instead of overlapping by repeating a whole paragraph — which does
    nothing when a piece is a single big paragraph — each new piece PREPENDS the
    previous piece's last ~overlap_words words. That guarantees a cushion every
    time a topic is split, no matter the paragraph sizes.
    """
    chunks: list[dict] = []
    n = len(blocks)
    i = 0
    carry_text = ""
    carry_pages: set[int] = set()

    while i < n:
        cur = []
        running = count_tokens(carry_text)
        j = i
        while j < n and (j == i or running + blocks[j][2] <= target):
            running += blocks[j][2]
            cur.append(blocks[j])
            j += 1

        body_text = " ".join(b[0] for b in cur)
        body_pages = {p for _, ps, _ in cur for p in ps}

        text = f"{carry_text} {body_text}".strip() if carry_text else body_text
        pages = sorted(carry_pages | body_pages)
        chunks.append({"text": text, "page_nos": pages, "token_estimate": count_tokens(text)})

        if j >= n:
            break

        # Cushion for the next piece: last ~overlap_words of THIS piece's fresh
        # body, tagged with the pages of the block it came from.
        words = body_text.split()
        carry_text = " ".join(words[-overlap_words:])
        carry_pages = set(cur[-1][1])
        i = j

    return chunks


def section_to_chunks(section: dict) -> list[dict]:
    blocks = section["blocks"]
    total = sum(t for _, _, t in blocks)
    if total <= WHOLE_SECTION_MAX:
        return [combine(blocks)]
    return pack_blocks(blocks, TARGET_TOKENS, OVERLAP_WORDS)


# ---------------------------------------------------------------------------
# Metadata from the CourtListener cluster
# ---------------------------------------------------------------------------


def pick_citation(cluster: dict) -> tuple[str, int]:
    """Return ("455 So. 2d 330", 330) from the cluster's citation list."""
    for c in cluster.get("citations") or []:
        if c.get("volume") and c.get("reporter") and c.get("page"):
            cite = f"{c['volume']} {c['reporter']} {c['page']}"
            start = int(c["page"]) if str(c["page"]).isdigit() else 0
            return cite, start
    return "?", 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description="Topic-aware chunker for court opinions.")
    ap.add_argument("raw_json", type=Path, help="data/raw/opinions/<slug>.json")
    ap.add_argument("--dry-run", action="store_true", help="Print the summary; don't write chunks.jsonl")
    args = ap.parse_args()

    if not args.raw_json.exists():
        sys.exit(f"Not found: {args.raw_json}")

    data = json.loads(args.raw_json.read_text(encoding="utf-8"))
    slug = data["slug"]
    opinion = data["opinion"]
    cluster = data.get("cluster") or {}

    html = opinion.get("html_lawbox") or opinion.get("html") or opinion.get("plain_text")
    if not html:
        sys.exit(f"No usable text field in {args.raw_json}")

    citation, start_page = pick_citation(cluster)
    case_name = cluster.get("case_name", "?")
    date_filed = cluster.get("date_filed", "?")
    docket_m = re.search(r"No\.\s*([\d-]+)", html)
    docket_nums = [docket_m.group(1)] if docket_m else []

    # Parse and grab the top-level blocks in document order.
    soup = BeautifulSoup(html, "html.parser")
    root = soup.find("div") or soup
    blocks = list(root.children)

    state = {"page": start_page}
    sections = build_sections(blocks, state)

    # Assemble chunk records, remembering which section each came from (for the
    # eyeball summary; the "section" field is ignored by embed_chunks.py).
    chunks: list[dict] = []
    for section in sections:
        for c in section_to_chunks(section):
            idx = len(chunks) + 1
            chunks.append({
                "chunk_id": f"{slug}__chunk-{idx:02d}",
                "doc_id": slug,
                "source_stem": slug,
                "doc_kind": "court-opinion",
                "doc_template": citation,
                "page_nos": c["page_nos"],
                "case_nums": docket_nums,
                "chunk_index": idx,
                "chunk_count": 0,  # filled below
                "token_estimate": c["token_estimate"],
                "char_count": len(c["text"]),
                "text": c["text"],
                "section": section["title"],
            })
    for c in chunks:
        c["chunk_count"] = len(chunks)

    # ---- Summary for eyeballing where the cuts landed ----
    print(f"Opinion : {case_name}  ({citation}, {date_filed})")
    print(f"Docket  : {docket_nums or '?'}   start page: {start_page}")
    print(f"Topics  : {len(sections)}   ->   Chunks: {len(chunks)}")
    print("-" * 78)
    for c in chunks:
        lo, hi = (c["page_nos"][0], c["page_nos"][-1]) if c["page_nos"] else (0, 0)
        preview = c["text"][:70].replace("\n", " ")
        print(f"[{c['chunk_index']:>2}] pp.{lo}-{hi:<4} {c['token_estimate']:>4} tok | {preview}")
    print("-" * 78)
    toks = sorted(c["token_estimate"] for c in chunks)
    print(f"token sizes  min={toks[0]}  median={toks[len(toks)//2]}  max={toks[-1]}")

    if args.dry_run:
        print("\n(dry-run: chunks.jsonl not written)")
        return

    out_dir = WEB_DIR / slug
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "chunks.jsonl"
    with out_path.open("w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    print(f"\nWrote {len(chunks)} chunks to {out_path.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
