# CaseFile AI

A retrieval-augmented research assistant for declassified criminal case files.
Designed to query FBI Vault PDFs (starting with the Ted Bundy archive) and
return cited answers, not vibes.

## Status

**Phase 1 — Data Prep: complete.** 65 of 85 pages from *Bundy Part 01* are
OCR'd, scored, and cleaned. Ready for chunking + vector indexing.

See [`docs/roadmap.md`](docs/roadmap.md) for the full multi-phase plan and
[`docs/data-prep-log.md`](docs/data-prep-log.md) for the complete record of
decisions, bugs, and fixes from this stage.

## What's Built

The data-prep pipeline turns a scanned FBI PDF into structured per-page
records, ready for chunking:

```
PDF ──▶ probe          (triage: text vs scanned vs garbage text layer)
    ──▶ ocr            (300 DPI, per-word confidence)
    ──▶ score_pages    (assign clean / skipped buckets)
    ──▶ clean_pages    (template-aware: FD-36, FD-350, 4-750, ...)
    ──▶ pages.jsonl    (one record per page, ready to chunk)
```

A fact-preservation guardrail compares pre- and post-cleaning text to ensure
no dates, dollar amounts, or case-file numbers are silently dropped.

## Quick Start (Windows)

### 1. System dependencies

- [Tesseract OCR 5.x](https://github.com/UB-Mannheim/tesseract/wiki) — installed to `C:\Program Files\Tesseract-OCR\`
- [Poppler](https://github.com/oschwartz10612/poppler-windows/releases) — extracted to `C:\Program Files\poppler\`

(The scripts fall back to these paths automatically if the binaries are not
on `PATH`.)

### 2. Python environment

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 3. API keys (only needed once chunking/query phase begins)

```powershell
copy .env.example .env
# Edit .env with your real Anthropic + Pinecone keys.
```

### 4. Run the pipeline

```powershell
# Drop your PDF into data/raw/, then:
python src/probe.py        data/raw/bundy-part-01.pdf
python src/tools_check.py
python src/ocr.py          data/raw/bundy-part-01.pdf
python src/score_pages.py  data/ocr/bundy-part-01/pages.jsonl --threshold 60
python src/clean_pages.py  data/ocr/bundy-part-01/pages.jsonl
```

Outputs land in `data/ocr/<pdf-name>/`.

## Project Layout

```
Case_File_AI/
├── src/                      # Pipeline code
│   ├── probe.py              # PDF triage (text / scanned / garbage)
│   ├── tools_check.py        # Tesseract + Poppler verification
│   ├── ocr.py                # Bulk OCR at 300 DPI
│   ├── score_pages.py        # Confidence threshold tuning
│   └── clean_pages.py        # Template-aware OCR cleaner
│
├── scripts/                  # Dev / audit tools
│   ├── inspect_unknowns.py   # Investigate untemplated pages
│   ├── inspect_flags.py      # Investigate guardrail flags
│   └── ocr_report.py         # OCR-quality report (md + csv)
│
├── docs/                     # Planning + design + decisions
│   ├── roadmap.md
│   ├── cleaner-spec.md
│   └── data-prep-log.md
│
├── tests/                    # (regression tests — coming)
├── data/                     # PDFs + OCR outputs (gitignored)
│   ├── raw/
│   └── ocr/
│
├── requirements.txt
├── .env.example
└── .gitignore
```

## Tech Stack

- **Python 3.13** — pipeline implementation
- **Tesseract 5.5** + **Poppler 26.02** — OCR and PDF rendering
- **Pinecone** (planned) — vector index with integrated `llama-text-embed-v2`
- **Anthropic Claude** (planned) — grounded answer generation with citations

## What's Next

- **Chunking** — split each cleaned page into ~500-token chunks with 100-token overlap
- **Indexing** — embed chunks via Pinecone integrated embedding, upsert with per-page metadata
- **Query** — question → retrieval → Claude → cited answer
- **Eval set** — 10 hard questions, scored for retrieval recall and citation correctness
