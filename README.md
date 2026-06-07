# CaseFile AI

Multi-agent criminal case research platform. Built in phases — see
`CaseFile-AI-Production-Roadmap.md` for the full plan.

**Current phase:** Phase 1 — working prototype (one PDF, queryable from the CLI).

## Setup

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\activate        # Windows PowerShell

# 2. Install dependencies
pip install -r requirements.txt

# 3. Add your API keys
copy .env.example .env        # then edit .env with real keys
```

Get keys from:
- Anthropic: https://console.anthropic.com
- Pinecone: https://app.pinecone.io

## Phase 1 progress

- [ ] **Step 0** — Probe the PDF: text or scanned image?
  - Download Bundy Part 1 from https://vault.fbi.gov into `data/raw/`
  - `python probe.py data/raw/bundy-part-01.pdf`
- [ ] **Step 1** — `query.py`: extract → chunk → embed → search → answer
- [ ] **Step 2** — Ask 10 hard questions, log what breaks (this becomes your eval set)

## Layout

```
Case_File_AI/
├── probe.py            # Step 0: is the PDF extractable text?
├── query.py            # Step 1: the RAG prototype (to be built)
├── data/raw/           # raw PDFs (gitignored)
├── requirements.txt
└── .env.example
```
