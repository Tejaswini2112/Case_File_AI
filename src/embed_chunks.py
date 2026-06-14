"""
Step 6 — Embed + upsert chunks to Pinecone.

Reads chunks.jsonl produced by chunk_documents.py and pushes every chunk to a
Pinecone serverless index that uses INTEGRATED EMBEDDING — meaning Pinecone
runs the embedding model server-side at upsert and query time. We never call
an embedding API ourselves; we send raw text + metadata and Pinecone does
the rest.

The script is intentionally "production-shaped at toy scale":
  - 53 chunks fits in a single batch, but we still loop in batches so swapping
    in a 50k-chunk PDF later is a config change, not a rewrite.
  - Upserts retry on 5xx with exponential backoff. 4xx errors are surfaced
    immediately because they indicate a bug in our payload.
  - Index creation is idempotent — re-running just no-ops if the index exists.
  - Vector IDs are stable composite keys (chunk_id) so re-runs overwrite in
    place instead of duplicating. This is decision #5 from the learning
    module — the single biggest source of "why are we seeing duplicate
    answers?" bugs in production RAG systems.
  - After writing, we read back the index stats to verify. Production habit:
    trust nothing.

The 7 design decisions from the learning module are annotated inline at the
places they show up in the code.

Usage:
    python src/embed_chunks.py data/ocr/bundy-part-01/chunks.jsonl
    python src/embed_chunks.py data/ocr/bundy-part-01/chunks.jsonl --dry-run
    python src/embed_chunks.py data/ocr/bundy-part-01/chunks.jsonl --recreate
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from pinecone import Pinecone, PineconeApiException

sys.stdout.reconfigure(encoding="utf-8")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Decisions 1, 2, 3, 7 from the learning module live in this block.
# These are module-level constants (not CLI args) on purpose: they describe
# the SHAPE OF THE INDEX, not per-run behavior. Letting them vary by run
# would mean two invocations could silently produce two incompatible indexes
# under the same name.

# Decision 1 + 2: Pinecone serverless (managed cloud-native, scales to zero).
PINECONE_CLOUD = "aws"
PINECONE_REGION = "us-east-1"  # the only region on the free Starter tier as of 2026

# Decision 3: integrated embedding model. llama-text-embed-v2 is asymmetric
# (trained for query↔passage retrieval, not for symmetric similarity), 1024
# dimensions. If we ever change this, every existing vector is incompatible
# and we need a brand new index — which is exactly what the -v1 suffix on
# the index name (decision 7) is there to enable.
EMBED_MODEL = "llama-text-embed-v2"

# Decision 3 + 4: which chunk field gets sent to the embedding model. Every
# other field becomes filterable metadata. We embed "text" because that's the
# only field with real semantic content; embedding "doc_kind" would be useless.
FIELD_MAP = {"text": "text"}

# Decision 7: index versioning. The -v1 suffix sets us up for the blue-green
# re-indexing pattern. When we change chunking or the embedding model, we
# create casefile-ai-v2, populate it, validate it against the golden eval set,
# then flip PINECONE_INDEX in .env. The old index stays around until we're
# confident, so rollback is one env-var change away. Never mutate prod.
DEFAULT_INDEX_NAME = "casefile-ai-v1"

# Decision 6: batching. Pinecone's upsert_records caps at 96 records per call.
# We use 90 to leave headroom — any single oversized record near the limit
# would otherwise reject the whole batch.
BATCH_SIZE = 90

# Retry policy for transient failures (5xx, network errors).
# Why 4 retries with exponential backoff (2s → 4s → 8s → 16s = ~30s ceiling):
# enough to ride out a brief Pinecone hiccup, not so much that a broken script
# wastes minutes before failing visibly. 4xx errors are NEVER retried — those
# are bugs in our payload and retrying just hides them.
MAX_RETRIES = 4
INITIAL_BACKOFF_SECONDS = 2

# Pinecone metadata constraints we care about (decision 4):
#   - Values must be: str, int, float, bool, or list[str]
#   - No nested objects, no list[int], no list[float]
#   - Total metadata size per record ≤ 40 KB
# We're well under the size limit (typical chunk metadata is <1 KB), but the
# list[int] restriction bites us on page_nos — we convert below.


# ---------------------------------------------------------------------------
# Load + transform chunks
# ---------------------------------------------------------------------------


def load_chunks(path: Path) -> list[dict]:
    """Read chunks.jsonl — one JSON object per line. Fail loudly if empty."""
    if not path.exists():
        sys.exit(
            f"Not found: {path}\n"
            f"Run: python src/chunk_documents.py <pages.jsonl>"
        )
    chunks = [
        json.loads(line)
        for line in path.open(encoding="utf-8")
        if line.strip()
    ]
    if not chunks:
        sys.exit(f"Empty: {path}")
    return chunks


def chunk_to_record(chunk: dict) -> dict:
    """
    Map our internal chunk dict → Pinecone's upsert_records wire format.

    The wire format for integrated embedding is flat:
        {
          "_id":   "<unique vector id>",         ← decision 5: stable composite
          "text":  "<the field that gets embedded>",  ← decision 3+4
          "<everything else>": <metadata value>, ← decision 4: filterable
        }

    Pinecone-specific gotchas handled here:
      - page_nos is list[int] in our jsonl. Pinecone metadata supports
        list[str] but NOT list[int], so we coerce. The stringified pages
        are still filterable with $in (e.g. retrieve only chunks that
        cover page "47") and still readable for citation rendering.
      - doc_template can be None on cover/unknown docs. Pinecone allows
        nulls only via field omission, so we default to "unknown" to keep
        the metadata schema uniform across all records.
    """
    return {
        # Decision 5: stable composite ID. Re-running this script overwrites
        # the same vector instead of inserting a duplicate. This is what makes
        # ingestion idempotent.
        "_id": chunk["chunk_id"],

        # Decision 3 + 4: this is the FIELD_MAP target — what Pinecone embeds.
        "text": chunk["text"],

        # Decision 4: metadata schema. Every field below exists because we've
        # already imagined a query that needs it for filtering or citation.
        "doc_id":         chunk["doc_id"],
        "source_stem":    chunk["source_stem"],
        "doc_kind":       chunk["doc_kind"] or "unknown",
        "doc_template":   chunk.get("doc_template") or "unknown",
        # list[int] → list[str] for Pinecone compatibility.
        "page_nos":       [str(p) for p in chunk["page_nos"]],
        "case_nums":      chunk.get("case_nums") or [],
        "chunk_index":    chunk["chunk_index"],
        "chunk_count":    chunk["chunk_count"],
        "token_estimate": chunk["token_estimate"],
        "char_count":     chunk["char_count"],
    }


# ---------------------------------------------------------------------------
# Pinecone connection
# ---------------------------------------------------------------------------


def connect() -> Pinecone:
    """
    Create a Pinecone client. Fails loudly with actionable instructions if
    the API key is missing or obviously wrong-shaped.

    The key-shape heuristic catches the most common beginner mistake: pasting
    the auto-generated index name (e.g. 'db-quickstart-xxxxx') from the
    Pinecone dashboard instead of the actual API key (which starts with
    'pcsk_'). Both are visible in the console; only one of them works.
    """
    load_dotenv()
    api_key = os.getenv("PINECONE_API_KEY")
    if not api_key:
        sys.exit(
            "PINECONE_API_KEY is not set.\n"
            "  1. Get a key from https://app.pinecone.io → API Keys\n"
            "  2. Add to .env:\n"
            "         PINECONE_API_KEY=pcsk_...\n"
            "         PINECONE_INDEX=casefile-ai-v1\n"
        )
    if not api_key.startswith("pcsk_"):
        print(
            f"WARNING: PINECONE_API_KEY does not start with 'pcsk_' "
            f"(got '{api_key[:12]}...'). This looks like an index name, not "
            f"an API key. Real keys are ~70 chars and start with pcsk_.",
            file=sys.stderr,
        )
    return Pinecone(api_key=api_key)


# ---------------------------------------------------------------------------
# Idempotent index setup
# ---------------------------------------------------------------------------


def ensure_index(pc: Pinecone, name: str, recreate: bool) -> str:
    """
    Make sure the index exists with our integrated-embedding configuration.

    Idempotent by design: re-running ensure_index() is safe. The CALLER can
    force a rebuild by passing --recreate, which is useful when iterating on
    the embedding model or field map — both are immutable after creation, so
    "edit then re-upsert" requires a full rebuild.

    Returns the index host URL (needed to construct the Index client below).
    """
    existing_names = {i.name for i in pc.list_indexes()}

    if name in existing_names and recreate:
        print(f"--recreate: deleting index '{name}' ...")
        pc.delete_index(name)
        # delete_index returns before Pinecone has actually finished tearing
        # down the index. Poll until it's gone — typically 5–15 seconds.
        for _ in range(30):
            if name not in {i.name for i in pc.list_indexes()}:
                break
            time.sleep(2)
        else:
            sys.exit(f"Index '{name}' didn't finish deleting after 60s.")
        existing_names.discard(name)

    if name not in existing_names:
        print(
            f"Creating index '{name}' "
            f"(model={EMBED_MODEL}, cloud={PINECONE_CLOUD}/{PINECONE_REGION}) ..."
        )
        pc.create_index_for_model(
            name=name,
            cloud=PINECONE_CLOUD,
            region=PINECONE_REGION,
            embed={
                "model": EMBED_MODEL,
                "field_map": FIELD_MAP,
            },
        )
        # Pinecone provisions the index asynchronously — describe_index().status
        # exposes a .ready bool. Poll until ready before we try to upsert.
        for _ in range(30):
            desc = pc.describe_index(name)
            if desc.status.ready:
                break
            time.sleep(2)
        else:
            sys.exit(f"Index '{name}' was not ready after 60s.")
        print("  index ready.")
    else:
        print(f"Index '{name}' already exists — reusing.")

    return pc.describe_index(name).host


# ---------------------------------------------------------------------------
# Upsert with retry
# ---------------------------------------------------------------------------


def upsert_batch_with_retry(index, namespace: str, batch: list[dict]) -> None:
    """
    Send one batch of records. Retry on transient errors only.

    The 4xx-vs-5xx distinction is intentional and worth memorizing:
      - 4xx (400, 401, 403, 422...) = OUR FAULT. Bad payload, bad auth, bad
        index name. Retrying just delays the inevitable failure and hides
        the real bug. Surface immediately.
      - 5xx (500, 502, 503, 504) + network errors = THEIR FAULT (or the
        network's). Transient. Back off and retry.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            index.upsert_records(namespace=namespace, records=batch)
            return
        except PineconeApiException as e:
            status = getattr(e, "status", None)
            # 4xx → don't retry, let it propagate.
            if status is not None and 400 <= status < 500:
                raise
            if attempt == MAX_RETRIES:
                raise
            backoff = INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1))
            print(
                f"  retry {attempt}/{MAX_RETRIES} after {backoff}s "
                f"(status={status}, err={e})",
                file=sys.stderr,
            )
            time.sleep(backoff)
        except Exception as e:
            # Network errors, DNS, TLS handshake, timeouts — all transient.
            if attempt == MAX_RETRIES:
                raise
            backoff = INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1))
            print(
                f"  retry {attempt}/{MAX_RETRIES} after {backoff}s "
                f"({type(e).__name__}: {e})",
                file=sys.stderr,
            )
            time.sleep(backoff)


# ---------------------------------------------------------------------------
# Batch loop
# ---------------------------------------------------------------------------


def upsert_all(index, records: list[dict], namespace: str) -> None:
    """Push every record in batches of BATCH_SIZE. Logs per-batch progress."""
    total = len(records)
    sent = 0
    for batch_no, start in enumerate(range(0, total, BATCH_SIZE), 1):
        batch = records[start : start + BATCH_SIZE]
        t0 = time.perf_counter()
        upsert_batch_with_retry(index, namespace, batch)
        dt = time.perf_counter() - t0
        sent += len(batch)
        print(
            f"  batch {batch_no}: upserted {len(batch)} records "
            f"in {dt:.1f}s  ({sent}/{total})"
        )


# ---------------------------------------------------------------------------
# Verification — trust nothing
# ---------------------------------------------------------------------------


def verify(index, namespace: str, expected_count: int) -> None:
    """
    After writing, read back. Pinecone's stats are eventually consistent —
    they typically catch up within a few seconds after upsert. We poll for
    up to 30 seconds before printing a warning, because in development a
    persistent gap usually means we upserted to the wrong namespace.
    """
    print("\nVerifying index ...")
    for attempt in range(1, 16):
        stats = index.describe_index_stats()
        ns_stats = stats.namespaces.get(namespace) if stats.namespaces else None
        ns_count = ns_stats.vector_count if ns_stats else 0
        if ns_count >= expected_count:
            print(
                f"  namespace '{namespace}' reports {ns_count} vectors "
                f"(expected {expected_count})."
            )
            return
        print(
            f"  attempt {attempt}: {ns_count}/{expected_count} visible — "
            f"waiting for stats to catch up ..."
        )
        time.sleep(2)
    print(
        f"WARNING: only {ns_count}/{expected_count} vectors visible after 30s. "
        f"Check the Pinecone console — most likely cause is a namespace mismatch.",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "chunks_path",
        type=Path,
        help="Path to chunks.jsonl produced by chunk_documents.py",
    )
    ap.add_argument(
        "--index",
        default=None,
        help=f"Override index name (default: $PINECONE_INDEX or '{DEFAULT_INDEX_NAME}')",
    )
    ap.add_argument(
        "--namespace",
        default="__default__",
        help="Pinecone namespace (default: __default__)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Load + transform chunks and print a sample record. Don't talk to Pinecone.",
    )
    ap.add_argument(
        "--recreate",
        action="store_true",
        help="Delete and recreate the index before upserting. Use when you've "
             "changed the embedding model or field_map.",
    )
    args = ap.parse_args()

    load_dotenv()
    index_name = (
        args.index
        or os.getenv("PINECONE_INDEX")
        or DEFAULT_INDEX_NAME
    )

    # ---- Phase 1: load + transform (cheap, runs even in dry-run) ----
    chunks = load_chunks(args.chunks_path)
    records = [chunk_to_record(c) for c in chunks]
    print(f"Loaded {len(chunks)} chunks → {len(records)} records.")
    print(f"Index: {index_name}    Namespace: {args.namespace}")

    if args.dry_run:
        sample = records[0]
        printable = {
            k: (v[:80] + "..." if isinstance(v, str) and len(v) > 80 else v)
            for k, v in sample.items()
        }
        print("\nSample record (first chunk):")
        print(json.dumps(printable, indent=2, ensure_ascii=False))
        print("\n(dry-run: skipping Pinecone connection)")
        return

    # ---- Phase 2: connect, ensure index, upsert, verify ----
    pc = connect()
    host = ensure_index(pc, index_name, recreate=args.recreate)
    index = pc.Index(host=host)

    print(f"\nUpserting {len(records)} records in batches of {BATCH_SIZE} ...")
    upsert_all(index, records, namespace=args.namespace)
    verify(index, namespace=args.namespace, expected_count=len(records))
    print("\nDone.")


if __name__ == "__main__":
    main()
