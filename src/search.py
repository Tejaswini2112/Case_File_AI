"""
Step 7 — Semantic search over the casefile-ai-v1 index.

Given a free-form question, returns the top-k most relevant chunks with
their similarity scores and metadata. This is the *retrieval* half of RAG;
step 8 (ask.py) will hand these chunks to Claude for grounded answering.

Because we used integrated embedding at upsert time, Pinecone embeds the
query server-side. We send raw text; the embedding, similarity search, and
metadata filtering all happen in a single API call.

Usage:
    python src/search.py "What evidence convicted Bundy?"
    python src/search.py "Chi Omega murders" --top-k 10
    python src/search.py "newspaper coverage" --doc-kind newspaper
    python src/search.py "Salt Lake City" --case-num 886895
    python src/search.py "redacted material" --doc-kind deletion-sheet
    python src/search.py "..." --filter '{"doc_kind": {"$eq": "teletype"}}'
    python src/search.py "..." --json    # machine-readable output
"""

import argparse
import json
import os
import sys

from dotenv import load_dotenv
from pinecone import Pinecone

sys.stdout.reconfigure(encoding="utf-8")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Top-k is the single most consequential dial in retrieval. Too low → miss
# relevant context. Too high → noisy context + more tokens to Claude → more
# cost + more chances to hallucinate. 5 is the conventional Phase 1 default;
# we'll likely tune this in step 9 once the eval set tells us the truth.
DEFAULT_TOP_K = 5

DEFAULT_NAMESPACE = "__default__"

# Chars of chunk text to show in human-readable output. The full text is
# always in the returned dict — this only affects terminal display.
SNIPPET_CHARS = 280


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def connect_to_index() -> tuple[object, str]:
    """
    Open the existing Pinecone index. Returns (index_client, index_name).

    Two env vars consumed:
      - PINECONE_API_KEY  same key embed_chunks.py used to write
      - PINECONE_INDEX    which index to read from (e.g. casefile-ai-v1)

    The host URL is fetched live via describe_index() rather than hardcoded.
    Pinecone may relocate indexes between regions (free tier moves happen),
    and the host is the one thing that changes when they do.
    """
    load_dotenv()
    api_key = os.getenv("PINECONE_API_KEY")
    index_name = os.getenv("PINECONE_INDEX", "casefile-ai-v1")

    if not api_key:
        sys.exit("PINECONE_API_KEY is not set in .env.")

    pc = Pinecone(api_key=api_key)
    try:
        host = pc.describe_index(index_name).host
    except Exception as e:
        sys.exit(
            f"Could not open index '{index_name}': {e}\n"
            f"Has embed_chunks.py been run to create it?"
        )
    return pc.Index(host=host), index_name


# ---------------------------------------------------------------------------
# Core search
# ---------------------------------------------------------------------------


def search(
    index,
    query_text: str,
    top_k: int = DEFAULT_TOP_K,
    filter: dict | None = None,
    namespace: str = DEFAULT_NAMESPACE,
) -> list[dict]:
    """
    Run a single semantic search. Returns hits ranked by score (highest first).

    The integrated-embedding flow inside Pinecone:
      1. Pinecone embeds `query_text` with llama-text-embed-v2 — the same
         model used at upsert. This is the asymmetric encoder: the query
         vector is produced by a DIFFERENT pass of the model than a document
         vector for the same text would be. Asymmetric models win on RAG
         because queries and passages have different shapes.
      2. If a metadata `filter` is provided, Pinecone narrows the candidate
         set BEFORE running similarity search. This is "pre-filtering" — the
         single highest-leverage feature in production retrieval. Filtering
         AFTER search is a beginner anti-pattern: you waste compute scoring
         vectors you were going to throw away anyway.
      3. Cosine similarity is computed against the candidate set; the top_k
         highest-scoring chunks come back with their metadata.

    About `_score`: a float roughly in [0, 1]. As a rough heuristic on this
    corpus, > 0.5 is usually relevant, < 0.3 is usually noise. These
    thresholds are dataset-specific — don't memorize, measure (step 9).
    """
    request: dict = {
        "inputs": {"text": query_text},
        "top_k": top_k,
    }
    if filter:
        request["filter"] = filter

    response = index.search(namespace=namespace, query=request)
    return [format_hit(h) for h in response.result.hits]


def format_hit(hit) -> dict:
    """
    Convert a Pinecone Hit object into our internal result shape — a plain
    dict that's easy to log, json.dump, or hand off to step 8 (ask.py).

    The page_nos round-trip is worth noticing: we stored them as list[str]
    because Pinecone metadata doesn't accept list[int] (Pinecone constraint,
    not ours). Here on the read path we own the consumer, so we flip them
    back to ints for clean downstream use. Closing the loop on the gotcha
    we hit in embed_chunks.py.
    """
    fields = dict(hit.fields)
    page_nos = fields.get("page_nos", [])
    try:
        page_nos = [int(p) for p in page_nos]
    except (TypeError, ValueError):
        pass  # leave as-is if a stray non-numeric snuck in

    return {
        "chunk_id":     hit.id,
        "score":        float(hit.score),
        "doc_id":       fields.get("doc_id"),
        "doc_kind":     fields.get("doc_kind"),
        "doc_template": fields.get("doc_template"),
        "source_stem":  fields.get("source_stem"),
        "page_nos":     page_nos,
        "case_nums":    list(fields.get("case_nums") or []),
        "text":         fields.get("text", ""),
    }


# ---------------------------------------------------------------------------
# Filter construction
# ---------------------------------------------------------------------------


def build_filter(
    doc_kind: str | None,
    case_num: str | None,
    raw_filter: str | None,
) -> dict | None:
    """
    Compose the metadata filter from CLI shortcuts + an optional raw JSON
    filter. Returns None if nothing was specified (= "search everything").

    Why both shortcuts and a raw escape hatch:
      - The two flags cover the filters you'll actually use 90% of the
        time. They're ergonomic; you'd never type `--filter '{...}'` to
        pick a doc kind.
      - The raw `--filter` lets you express anything Pinecone's filter DSL
        supports without us having to grow a flag for every operator.

    Pinecone filter DSL is mongo-like: $eq, $ne, $in, $nin, $gt, $lt,
    $and, $or. Lists like case_nums use $in (the chunk's list intersects
    the filter's list).
    """
    clauses = []
    if doc_kind:
        clauses.append({"doc_kind": {"$eq": doc_kind}})
    if case_num:
        clauses.append({"case_nums": {"$in": [case_num]}})
    if raw_filter:
        try:
            clauses.append(json.loads(raw_filter))
        except json.JSONDecodeError as e:
            sys.exit(f"--filter must be valid JSON: {e}")

    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


# ---------------------------------------------------------------------------
# Pretty-print
# ---------------------------------------------------------------------------


def print_results(results: list[dict], query: str) -> None:
    if not results:
        print(f"\nNo results for: {query!r}")
        return

    print(f"\nQuery: {query!r}")
    print(f"Found {len(results)} hits:\n")

    for i, r in enumerate(results, 1):
        pages = ",".join(str(p) for p in r["page_nos"]) or "-"
        case_str = ",".join(r["case_nums"][:2]) if r["case_nums"] else "-"
        snippet = r["text"][:SNIPPET_CHARS]
        if len(r["text"]) > SNIPPET_CHARS:
            snippet += "..."

        print(
            f"  [{i}] score={r['score']:.3f}  doc={r['doc_id']}  "
            f"kind={r['doc_kind']}  pages=[{pages}]  case#={case_str}"
        )
        print(f"      {snippet}")
        print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Semantic search over the casefile-ai index."
    )
    ap.add_argument("query", help="The question or search phrase")
    ap.add_argument(
        "--top-k",
        type=int,
        default=DEFAULT_TOP_K,
        help=f"Number of results to return (default: {DEFAULT_TOP_K})",
    )
    ap.add_argument(
        "--doc-kind",
        help="Filter by document kind: teletype, newspaper, cover, "
             "deletion-sheet, form, loose, teletype-cont",
    )
    ap.add_argument(
        "--case-num",
        help="Filter by case file number (digits only, e.g. 886895)",
    )
    ap.add_argument(
        "--filter",
        help="Raw Pinecone filter JSON, e.g. '{\"doc_kind\": {\"$ne\": \"cover\"}}'",
    )
    ap.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    ap.add_argument(
        "--json",
        action="store_true",
        help="Print results as a JSON array (for piping to ask.py in step 8)",
    )
    args = ap.parse_args()

    index, index_name = connect_to_index()
    filter_dict = build_filter(args.doc_kind, args.case_num, args.filter)

    results = search(
        index,
        query_text=args.query,
        top_k=args.top_k,
        filter=filter_dict,
        namespace=args.namespace,
    )

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        print_results(results, args.query)


if __name__ == "__main__":
    main()
