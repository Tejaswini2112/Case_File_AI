# CaseFile AI

Ask questions about declassified criminal case files and get answers traced to
the page they came from — or told plainly that the files do not say.

```
$ python src/agents/ask.py "How did Bundy escape from custody?" --case bundy --top-k 4

**First escape (Aspen, Pitkin County Courthouse):** Bundy jumped from a
second-floor window in the back of the courtroom when he was left unguarded
during a recess in a pretrial hearing [bundy-part-01__doc-026, p.41]. [...]
When asked why he escaped, Bundy replied, "I didn't want to go back to jail.
It was just too pretty outside" [bundy-part-01__doc-026, p.41].

**Second escape (Glenwood Springs, Garfield County Jail):** Bundy removed a
light fixture from the ceiling of his cell, slid through the resulting 12-inch
hole, crawled across the ceiling space, dropped into the jailer's apartment,
and walked out the front door [bundy-part-02__doc-010, p.15;
bundy-part-02__doc-012, p.17]. Notably, other inmates had reported that Bundy
was crawling in the space above his cell, and the sheriff's department had
called a welder to secure the fixture — but the welder never arrived before
Bundy escaped [bundy-part-02__doc-010, p.15].

Sources used (4 excerpts retrieved):
  [1] bundy-part-02__doc-012  pages=[17]     kind=newspaper  score=0.513
  [2] bundy-part-01__doc-016  pages=[24,25]  kind=newspaper  score=0.465
  [3] bundy-part-02__doc-010  pages=[15]     kind=newspaper  score=0.463
  [4] bundy-part-01__doc-026  pages=[41]     kind=newspaper  score=0.457

Model: claude-sonnet-4-6  |  tokens: 3067 in / 445 out  |  cost: ~$0.0159
```

That is a real run, lightly trimmed for length. Note that the welder detail is
in the files and not in general knowledge — it is the kind of thing this system
exists to surface.

Ask it something the files do not cover and it refuses rather than guessing,
at no cost, because no model is called at all.

---

## Why this is harder than it looks

The corpus is **339 pages of 1970s FBI paper**, released under the Freedom of
Information Act and scanned badly:

- Typewritten carbon copies, OCR'd at 300 dpi. Median per-page confidence is
  **77.6 out of 100**; the floor is zero. `cup` reads as `cvp`, `torn` as `tom`.
- **22 pages are FOIA deletion sheets** — placeholders where content was
  withheld. They interrupt documents mid-way.
- Documents span pages with no machine-readable link between them. A single
  teletype can run across ten physical pages, two of which are withholdings.
- **Nothing may be answered from general knowledge.** The model knows a great
  deal about Ted Bundy; almost none of it is in these files. An answer that
  looks right but is not in the documents is the failure mode this system
  exists to prevent.

So most of the work is not the language model. It is turning damaged paper into
something that can be cited.

## What is measured

| | |
|---|---|
| Eval suite | **15 / 17** questions passing |
| Citations grounded | **12 / 12** — every citation in every answer points at a document actually retrieved |
| Out-of-corpus questions | **refused, 3 / 3** — no hallucinated answers |
| Cost per answered question | **$0.0108** average, measured across the suite |
| Cost per refusal | **$0.00** — refusal happens before the model is called |
| Ingestion, peak memory | 2,678 MB → **356 MB**, and flat regardless of document length |
| Ingestion, throughput | **2.4× on 8 workers** (12 workers is slower than 4 — measured, see the scaling log) |
| Unit tests | 43, running in ~2s with no network and no cost |

Every figure is reproducible from this repo. The two eval failures are
described under [Known limits](#known-limits) rather than rounded away.

## How it works

**Ingestion** turns a source PDF into citable chunks. Eight stages, resumable
at page level, parallel across cores:

```
probe    is this a real text PDF or a scan?
ocr      render + Tesseract, per-page confidence
score    grade page quality, hold the file if the scan does not fit
clean    strip boilerplate, normalise redactions, preserve facts
group    assemble pages into documents, classify each
correct  apply human judgements this case needs (corrections/<case>.json)
chunk    ~500 tokens, never crossing a document, page numbers preserved
embed    upsert to Pinecone, stable ids so re-runs overwrite
```

**Retrieval** searches the index with metadata pre-filtering — by case, by
document kind, by case-file number — then applies a refusal threshold before
any model is called.

**Answering** hands the surviving excerpts to Claude under a prompt that
forbids general knowledge and requires an inline `[doc-id, p.N]` citation for
every claim.

The reasoning behind each of those choices is in
[`docs/design.md`](docs/design.md).

## Quick start

Windows, PowerShell. Requires Tesseract and Poppler on PATH, and Python 3.13.

```powershell
# 1. Environment
py -3.13 -m venv .venv
.venv\Scripts\pip.exe install -r requirements.txt -r requirements-dev.txt

# 2. Keys — copy .env.example to .env and fill in
#    ANTHROPIC_API_KEY, PINECONE_API_KEY, PINECONE_INDEX
#    (COURTLISTENER_TOKEN is needed only for the court-opinion path)

# 3. Ingest a folder of PDFs for one case
.venv\Scripts\python.exe -m src.ingestion.batch data\cases\bundy\raw\scans\ `
    --case bundy --threshold 60 --workers 8

# 4. Ask
.venv\Scripts\python.exe src\agents\ask.py "What evidence was found in the car?" --case bundy
```

### The web interface

```powershell
.venv\Scripts\python.exe -m uvicorn src.api.app:app --reload   # API on :8000
cd web; npm install; npm run dev                                # UI on :5173
```

The page shows the answer with each citation clickable, opening the excerpt the
model actually read alongside its retrieval score, drawn against the refusal
cut-off.

### Adding a case

1. Register it in [`src/cases.py`](src/cases.py)
2. Put its PDFs in `data/cases/<case>/raw/scans/`
3. Run the batch command above with `--case <case>`
4. Judgements no rule can make go in `corrections/<case>.json`

Court opinions come from a separate path —
[`fetch_opinions.py`](src/ingestion/fetch_opinions.py) pulls them from the
CourtListener API, and [`chunk_opinions.py`](src/ingestion/chunk_opinions.py)
chunks them by legal topic rather than by page.

## Project layout

```
src/
├── cases.py               registered cases; one source for ingestion and the API
├── paths.py               where data lives; CASEFILE_DATA_ROOT relocates it
├── ingestion/             probe, ocr, score, clean, group, correct, chunk
│                          + batch.py (a folder at a time) and pipeline.py (one PDF)
├── retrieval/             embed_chunks.py, search.py
├── agents/ask.py          the RAG loop: retrieve, decide, generate
└── api/app.py             FastAPI service over the same loop the eval scores
web/                       React + TypeScript + Vite front end
corrections/<case>.json    human judgements, in git because they cannot be regenerated
tests/                     43 unit tests + the eval suite
docs/                      design notes, the ingestion engineering log, data-prep log
data/cases/<case>/         sources and derived artifacts (gitignored)
```

## Known limits

Recorded rather than fixed, because each is a real trade-off:

- **Two eval questions fail.** Case-number lookups (*"what is file 88-6895
  about?"*) score just below the refusal threshold despite retrieving the right
  documents; and pure paraphrase with no shared vocabulary falls below semantic
  resolution. In both the system refuses honestly instead of guessing. The fix
  is metadata-aware confidence, not a lower threshold.
- **93 chunks are still classified `loose`** — documents whose form header the
  template detector missed. Only `bundy-part-01` has been curated so far.
- **Page images are not rendered.** The web interface shows a labelled
  placeholder rather than the scan.
- **Per-word OCR confidence is discarded.** Tesseract returns it; `ocr.py`
  averages it per page and drops the rest, so misread words cannot be
  highlighted without re-running OCR.
- **One case so far.** The corpus is scoped by case and the filter works, but
  cross-case bleed cannot be measured until a second case exists.
- **CI does not build the front end.** Only `src/` and `tests/` are linted and
  tested.

## Tech stack

| Layer | |
|---|---|
| Ingestion | Python 3.13, Tesseract, Poppler, PyMuPDF |
| Index | Pinecone serverless, `llama-text-embed-v2` integrated embedding |
| Answering | Claude Sonnet 4.6 |
| API | FastAPI + pydantic |
| Front end | React 18, TypeScript, Vite, Tailwind 4 |
| CI | GitHub Actions — ruff + pytest on every push |

## Further reading

- [`docs/design.md`](docs/design.md) — the decisions and their reasoning
- [`docs/ingestion-scaling.md`](docs/ingestion-scaling.md) — what broke as this
  grew, how it was measured, what changed
- [`docs/data-prep-log.md`](docs/data-prep-log.md) — OCR and cleaning decisions
- [`docs/cleaner-spec.md`](docs/cleaner-spec.md) — the cleaning rules
