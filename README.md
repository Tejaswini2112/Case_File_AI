# CaseFile AI

A retrieval-augmented research assistant for declassified criminal case files.
Queries FBI Vault PDFs (starting with the Ted Bundy archive) and returns cited
answers grounded in primary-source documents.

## Status

**Phase 1 — Complete.** End-to-end RAG pipeline shipped: scanned PDF → OCR
→ cleaned chunks → vector index → cited answer. Validated by a 10-question
eval suite.

```text
$ python src/agents/ask.py "What evidence was used against Bundy?"

Physical evidence collected from a 1966 Ford Station Wagon included a
large clear plastic drinking cup, a Meadow Gold milk carton, a Fig cookie
wrapper, two Camel cigarette wrappers, two Marlborough cigarette
wrappers, a torn check, paper stickers from a Christian Book Store, a
grocery store receipt, a Polaroid negative, a Kentucky Fried Chicken
box [...] and a latent fingerprint taken from the driver's side window
[bundy-part-01__doc-003, p.6; bundy-part-01__doc-003, p.7].
[...]
```

See [`docs/roadmap.md`](docs/roadmap.md) for the full multi-phase plan and
[`docs/data-prep-log.md`](docs/data-prep-log.md) for the OCR/cleaning decisions log.

## Eval Baseline (Phase 1 exit)

Run via `python tests/run_eval.py`. 10 golden Q&A pairs covering subject
match, narrative synthesis, specific-fact recall, paraphrase, case-number
lookup, FOIA-deletion handling, and three out-of-corpus refusal tests.

| Metric | Result |
|---|---|
| Pass rate | 8 / 10 |
| Avg cost per query (Sonnet 4.6) | ~$0.006 |
| Refusal correctness on out-of-corpus | 3 / 3 (no hallucinated answers) |

### Known limits (intentionally not fixed in Phase 1)

- **Case-number queries** (e.g. *"What is case file 88-6895 about?"*) score
  just below the refusal threshold despite correct retrieval. Phase 2 fix:
  metadata-aware confidence override when query case-number matches chunk
  metadata.
- **Pure-paraphrase queries** with no contextual anchor (e.g. *"Tell me
  about items collected as proof"*) fall below semantic resolution.
  System refuses honestly rather than guess.

Both are documented in `tests/eval_set.jsonl` and surface deliberately
through the eval runner — *failures are signal, not bugs*.

## Pipeline

```
PDF
 │
 ▼  probe              triage: real text PDF / scanned / garbage text layer
 ▼  ocr                300 DPI Tesseract, per-word confidence
 ▼  score_pages        clean/skipped buckets by confidence + letter ratio
 ▼  clean_pages        template-aware cleaner (FD-36 / FD-350 / 4-750 / ...)
 ▼  group_documents    multi-page documents grouped under shared doc_id
 ▼  chunk_documents    doc-aware chunks with overlap + FOIA placeholder synthesis
 ▼  embed_chunks       Pinecone serverless + integrated llama-text-embed-v2
 ▼  search             top-k semantic retrieval with metadata pre-filtering
 ▼  ask                refusal guardrail + Claude Sonnet 4.6 with citation prompt
```

A fact-preservation guardrail in `clean_pages.py` compares pre- and
post-cleaning text to ensure no dates, dollar amounts, or case-file
numbers are silently dropped during normalization.

A retrieval-score refusal guardrail in `ask.py` prevents Claude from
being given weak context — the single most impactful production defense
against RAG hallucination.

## Quick Start (Windows)

### 1. System dependencies

- [Tesseract OCR 5.x](https://github.com/UB-Mannheim/tesseract/wiki) — installed to `C:\Program Files\Tesseract-OCR\`
- [Poppler](https://github.com/oschwartz10612/poppler-windows/releases) — extracted to `C:\Program Files\poppler\`

Scripts auto-fall-back to these paths if the binaries aren't on `PATH`.

### 2. Python environment

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 3. API keys

```powershell
copy .env.example .env
```

Then edit `.env`:

```
ANTHROPIC_API_KEY=sk-ant-...
PINECONE_API_KEY=pcsk_...
PINECONE_INDEX=casefile-ai-v1
```

### 4. One-time data prep on a fresh PDF

Drop the PDF in `data/raw/`, then:

```powershell
python src\probe.py            data\raw\bundy-part-01.pdf
python src\ocr.py              data\raw\bundy-part-01.pdf
python src\score_pages.py      data\ocr\bundy-part-01\pages.jsonl --threshold 60
python src\clean_pages.py      data\ocr\bundy-part-01\pages.jsonl
python src\group_documents.py  data\ocr\bundy-part-01\pages.jsonl
python src\chunk_documents.py  data\ocr\bundy-part-01\pages.jsonl
python src\embed_chunks.py     data\ocr\bundy-part-01\chunks.jsonl
```

### 5. Ask questions

```powershell
python src\ask.py "What evidence was used against Bundy?"
python src\ask.py "Why did Bundy escape from Aspen?" --top-k 8
python src\ask.py "What was withheld in this release?"
python tests\run_eval.py
```

## Project Layout

```
Case_File_AI/
├── src/                       # Pipeline + RAG code
│   ├── probe.py               # PDF triage (text / scanned / garbage layer)
│   ├── tools_check.py         # Tesseract + Poppler verification
│   ├── ocr.py                 # Bulk OCR at 300 DPI
│   ├── score_pages.py         # Confidence threshold tuning
│   ├── clean_pages.py         # Template-aware OCR cleaner
│   ├── group_documents.py     # Multi-page document grouping
│   ├── chunk_documents.py     # Document-aware chunking with overlap
│   ├── embed_chunks.py        # Pinecone index setup + chunk upsert
│   ├── search.py              # Semantic retrieval CLI
│   └── ask.py                 # RAG query with citation + refusal guardrail
│
├── tests/                     # Regression eval suite
│   ├── eval_set.jsonl         # 10 golden Q&A pairs
│   └── run_eval.py            # Eval runner with 4 checks per question
│
├── scripts/                   # Dev / audit tools
│   ├── inspect_unknowns.py
│   ├── inspect_flags.py
│   ├── inspect_chunks.py
│   ├── ocr_report.py
│   └── ...
│
├── docs/
│   ├── roadmap.md             # Multi-phase architecture plan
│   ├── cleaner-spec.md
│   └── data-prep-log.md       # Decisions, bugs, fixes from Phase 1
│
├── data/                      # gitignored — raw PDFs + OCR outputs
│   ├── raw/
│   └── ocr/
│
├── requirements.txt
├── .env.example
└── .gitignore
```

## Tech Stack

- **Python 3.13** — pipeline + RAG implementation
- **Tesseract 5.5** + **Poppler 26.02** — OCR + PDF rendering
- **Pinecone serverless** — vector index with integrated `llama-text-embed-v2` (1024 dims)
- **Anthropic Claude Sonnet 4.6** — grounded answer generation with strict citation prompt
- **Retrieval-score refusal guardrail** — `ask.py` declines to answer when top-1 score < 0.30, preventing hallucination on out-of-corpus queries

## What's Next (Phase 2+)

- **Reranking** — retrieve top-20, rerank to top-5 with cross-encoder for precision
- **Hybrid retrieval** — combine vector similarity with BM25 keyword overlap
- **Query rewriting** — Haiku-powered rewriting of casual queries into precise search terms
- **Semantic caching** — Redis-backed cache of full responses for similar queries
- **Multi-corpus ingestion** — extend beyond Bundy Part 01
- **Web UI** (Next.js) — replace CLI with streaming chat interface
- **LangFuse tracing** — observability for every Claude call (cost, latency, faithfulness)

See [`docs/roadmap.md`](docs/roadmap.md) for the full progression and rationale.
