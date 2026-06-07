# CaseFile AI — Data Preparation Log (Bundy Part 01)

> Complete record of the data-prep stage: from raw FBI Vault PDF to ~50 cleaned, chunk-ready pages. Captures what was built, what was decided, what broke and how it was fixed.

---

## Status

- **Stage**: §5A data prep — **COMPLETE**
- **Next**: Chunking + Pinecone embedding (Roadmap Phase 1, Day 3–4)
- **Date completed**: 2026-06-07

---

## The Document

- **Source**: Bundy Part 01, FBI Vault (`vault.fbi.gov`)
- **Pages**: 85
- **Nature**: Scanned image PDF (1970s typewritten FBI files)
- **Embedded text layer**: Garbage — 51% ASCII-letter ratio, broken ToUnicode CMap. Page 10 example: `'A L I/&#39 0 1977 i \x1cH 1 Z4 \x01 4 1 i w 3 1 \x18!'`

---

## Pipeline Built

```
PDF
 │
 ▼  probe.py            → diagnose: is this a real text PDF or a scan?
 ▼  tools_check.py      → verify Tesseract + Poppler + Python deps
 ▼  ocr.py              → OCR every page at 300 DPI → page_NNN.txt + pages.jsonl
 ▼  score_pages.py      → confidence histogram → assign clean/skipped buckets
 ▼  inspect_unknowns.py → investigate pages whose template couldn't be detected
 ▼  clean_pages.py      → template-aware cleaner → clean_text + diagnostics
 ▼  inspect_flags.py    → audit pages flagged by fact-preservation guardrail
 ▼
ready for chunking
```

---

## Scripts (in execution order)

### 1. `probe.py` — PDF triage

**Purpose**: Decide if a new PDF needs OCR or can be parsed directly.

**Result on Bundy Part 01**: 🚨 TEXT LAYER IS GARBAGE. Average chars/page was high (1183) but only 51% were letters. The naive "char count > 200 → it's text" rule was a lie — added an **ASCII letter-ratio check** as the honest signal.

**Output verdict**:
- ≥200 chars AND ≥55% letters → TEXT PDF (skip OCR)
- ≥200 chars AND <55% letters → 🚨 GARBAGE LAYER (re-OCR from pixels)
- <200 chars → SCANNED IMAGE (OCR needed)

---

### 2. `tools_check.py` — Toolchain verification

**Purpose**: Confirm Tesseract + Poppler are reachable from Python before building `ocr.py`.

**Checks**:
1. `pytesseract` + `pdf2image` importable
2. Tesseract binary reachable (with **auto-fallback** to `C:\Program Files\Tesseract-OCR\tesseract.exe` if not on PATH)
3. Input PDF exists
4. Poppler reachable (with **auto-fallback** to `C:\Program Files\poppler\poppler-*\Library\bin`)
5. End-to-end OCR on page 1

**Installed**: Tesseract 5.5.0 (UB Mannheim) + Poppler 26.02.0 (oschwartz10612).

---

### 3. `ocr.py` — Bulk OCR

**Purpose**: OCR every page; capture text + confidence + structural metadata.

**Choices made**:
- **300 DPI** rendering — sweet spot for typewritten text
- **Single `image_to_data` call per page** — gives text AND per-word confidence in one pass (~half the cost of two calls)
- **Line reconstruction** from `(block, paragraph, line)` indices preserves layout
- **Confidence = mean of words with conf ≥ 0** (Tesseract returns -1 for non-word blocks)
- **FOIA exemption codes** (`b6`, `b7C`, etc.) captured as a per-page list

**Output**:
- `data/ocr/bundy-part-01/page_NNN.txt` (one per page)
- `data/ocr/bundy-part-01/pages.jsonl` (one row per page)

**Per-page record schema**:
```json
{
  "page_no": 4,
  "source_file": "data/raw/bundy-part-01.pdf",
  "text_path": "data/ocr/bundy-part-01/page_004.txt",
  "char_count": 1162,
  "letter_ratio": 0.639,
  "ocr_confidence": 68.6,
  "redaction_markers_found": [],
  "raw_text": "..."
}
```

**Runtime**: ~5 min total for 85 pages (~2 min render + ~2.5 min OCR).

---

### 4. `score_pages.py` — Threshold tuning

**Purpose**: Look at the per-page confidence distribution and pick a clean/skipped threshold.

**Confidence histogram** (Bundy Part 01):
```
20-29  #########       (7)
30-39  #####           (4)
40-49  #######         (5)
50-59  ####            (3)  ← soft gap
60-69  ############    (9)
70-79  ########################################  (30) ← healthy mode
80-89  #############################  (22)
90-99  #####           (4)
```

Not cleanly bimodal — continuous spectrum with a clear "good" peak at 70–85. Soft gap at 50–59.

**Threshold chosen**: `ocr_confidence >= 60 AND letter_ratio >= 0.55` → `clean`, else `skipped`.

**Result**: 65 clean / 20 skipped.

---

### 5. `clean_pages.py` — Template-aware cleaner

**Purpose**: Strip FBI form furniture without losing case facts. Routes deletion sheets out; produces `clean_text` ready for chunking.

**Architecture**: Two layers.
- **Layer 1**: Detect FBI form template by fuzzy regex on form number
- **Layer 2**: Apply per-template handler, then general pattern rules, then normalize, then fact-preservation guardrail

**Templates handled**:

| Template | Detection | Handler |
|---|---|---|
| `FD-36` | First-page teletype with FD-36 header | Cut body between first/last `BT` markers; **fallback**: pattern-strip named furniture blocks if BT garbled |
| `FD-36-cont` | `PAGE TWO/THREE/... DN NN-NNNNN` | Cut body from header line (kept as provenance) to closing `BT` |
| `FD-350` | Newspaper clipping form | Extract footer metadata (newspaper/date/title/author) BEFORE stripping, then truncate at first labeled field |
| `4-750` | FOIPA Deleted-Page sheet | **Route to missing-info**; extract exemption codes and reference number; no chunking |
| `FD-xxx` | Other FBI form numbers (FD-65, FD-302, FD-320, FD-479, etc.) | Light strip — form header only; keep field labels and values |
| `unknown` | No form number detected (court forms, memos, leads, cover sheets) | Conservative: general rules only, no structural cut |

**Layer 2 general rules** (every page):
- Strip control characters (Unicode Cc/Cf)
- Drop lines with letter ratio < 40% ("soup")
- Drop 1–3 char orphan lines (with keeplist for "I", "A", "FM", "TO", "RE", "BT", "FBI", "USA", "AKA", "SA")
- Strip `SEARCHED/SERIALIZED/INDEXED/FILED` stamp anywhere
- **Inline `[REDACTED]` labeling** for tokens with stray symbols (`@`, `{`, `}`, `::`) or high non-alphanumeric ratio
- Whitespace normalization + hyphen rejoin (run LAST)

**Fact-preservation guardrail** (safety net):
- Before cleaning: extract from `raw_text` all dates, dollar amounts, case file numbers (per-document, no hardcoded values)
- After cleaning: verify each survives in `clean_text`; flag any losses

**CLI**:
```
python clean_pages.py <pages.jsonl>                       # full run, writes back
python clean_pages.py <pages.jsonl> --detect-only          # template distribution only
python clean_pages.py <pages.jsonl> --pages 4,15,47        # smoke test on named pages
```

---

### 6. Investigation tools

- **`inspect_unknowns.py`**: prints first 5 non-blank lines of every "unknown" page. Used to discover that 33 unknowns were actually a mix of teletype continuations, garbled form numbers, court docs, and one-off memos.
- **`inspect_flags.py`**: prints every page that tripped the fact-preservation guardrail, with raw-text context for each lost fact. Used to distinguish boilerplate false-flags from real losses.

---

## Final Template Distribution (65 clean pages)

| Template | Count | Disposition |
|---|---|---|
| FD-36 (teletype, first page) | 4 | Cleaned via BT cut or fallback pattern-strip |
| FD-36-cont (continuation) | 6 | Cleaned via PAGE header → BT cut |
| FD-350 (newspaper clipping) | 10 | Metadata extracted, body kept |
| FD-xxx (other FBI forms) | 5 | Light strip of form header |
| 4-750 (deletion sheet) | 15 | Routed to missing-info (no chunking) |
| unknown (court docs, memos, leads) | 25 | General rules only |

**On disk**:
- `pages.jsonl` — 65 rows with `clean_text`, template, metadata, guardrail flags
- `data/ocr/bundy-part-01/clean/` — 50 per-page text files
- Deletion sheets have `clean_text=""` and `routed_to_missing_info=true` with exemption metadata

---

## Final Per-Page Schema

```json
{
  "page_no": 4,
  "source_file": "data/raw/bundy-part-01.pdf",
  "text_path": "data/ocr/bundy-part-01/page_004.txt",
  "char_count": 1162,
  "letter_ratio": 0.639,
  "ocr_confidence": 68.6,
  "redaction_markers_found": [],
  "raw_text": "...",
  "bucket": "clean",                  // from score_pages.py
  "template": "FD-36",                // from clean_pages.py
  "template_confidence": "low",       // 'low' = pattern-strip fallback used
  "extracted_metadata": {},           // FD-350 newspaper/date/title, 4-750 exemptions
  "redactions_inserted": 5,
  "boilerplate_lines_removed": 27,
  "routed_to_missing_info": false,
  "cleaning_flags": [],               // empty unless guardrail caught a loss
  "clean_text": "..."                 // what chunking will read
}
```

---

## Key Decisions

| Decision | Choice | Why |
|---|---|---|
| OCR scoring signal | Tesseract confidence (primary) + letter-ratio (secondary) | Catch different failure modes — confidence catches struggle, ratio catches symbol-soup with high confidence |
| Bucket count | Two (clean/skipped), MVP | All raw OCR + scores persisted, third bucket can be reconstructed later |
| `[REDACTED]` markers | **Inline** in chunk text | Model sees the gap at inference time, can say "withheld" instead of inventing |
| Cleaning approach | **Template-aware**, not statistical-repetition | Documents are templated FBI forms; rules carry across Bundy Parts 02/03 |
| OCR char substitutions (`rn→m` etc.) | **Deferred** | Too dangerous naively ("torn"→"tom"). Revisit only if retrieval failure traces to it |
| Threshold | 60 (after looking at histogram, spot-checking ±5) | Soft gap at 50–59; preserves substantive content above |
| Metadata storage | JSONL on disk (Phase 1) | Phase 1 roadmap forbids databases; schema is the contract, Postgres comes in Phase 3 |

---

## Bugs Found and Fixed

### Discovered via `probe.py`
1. **Naive char-count verdict lies on garbage text layers** → added letter-ratio check

### Discovered via Windows / cross-environment friction
2. **Windows console cp1252 chokes on emoji** in OCR/script output → `sys.stdout.reconfigure(encoding="utf-8")`
3. **Tesseract/Poppler not on PATH** when subprocess inherits stale env → auto-fallback to default Windows install paths

### Discovered via the first smoke test
4. **Soup-line filter dropped real data** (`11/24/78` alone on a line, 0% letters) → `line_contains_fact()` protects lines with dates/dollars/case numbers
5. **Exemption regex missed parenthesized variants** like `(b)(7)(A)` → second regex alternative + `normalize_exemption()` to dedupe `b7C` and `(b)(7)(C)`

### Discovered via the `--detect-only` investigation of 33 unknowns
6. **Form-number regex too strict on separator** — missed `FD.350` (OCR read dash as period) → allow `[-.]`
7. **Form-number regex required 2-3 digits** — missed `FD-4` → allow `\d{1,3}`
8. **No template type for teletype continuations** (`PAGE TWO DN 88-10975`) → new `FD-36-cont` template + handler

### Discovered via the second smoke test (page 4 — riskiest)
9. **FD-36 fallback didn't strip furniture when BT garbled** — pages with mangled BT markers left messy → added named pattern-strip fallback (FD-36 header line, TRANSMIT VIA/PRECEDENCE/CLASSIFICATION block, checkbox keyword lines, GPO footer, `(Number)/(Time)` labels)
10. **FD-36 fallback over-stripped `TO DENVER ROUTINE`** — `Routine` keyword in pattern matched routing lines → dropped `Routine`/`Priority`/`Immediate`/`SECRET` from the standalone keyword set; kept `Teletype`/`Facsimile`/`Airtel`/`TOP SECRET`/`CONFIDENTIAL`/`EFTO`
11. **Continuation page stripped its own header losing case number** (`88-10975` lost from `PAGE TWO DN 88-10975 CLEAR`) → kept the header line as light provenance (same convention as FM/TO routing)

### Discovered via the flag audit (`inspect_flags.py`)
12. **FD-36 fallback stripped body sentences containing "teletype"** (e.g., `Re Salt Lake City teletype dated June 10, 1977`) → protect lines containing facts from the fallback strip (same principle as soup filter)
13. **Redaction labeler ate tokens with case numbers** (`(88-6895}—.` flagged because of `}`) → protect tokens containing facts from redaction labeling
14. **FD-350 metadata pattern matched body prose** — bare `City` alternative matched `Salt Lake City` in article body, triggering truncation that nuked entire article → require `^...colon` anchoring for all field labels (`re.MULTILINE`)

---

## Final Guardrail Status

After all fixes, **5 of 85 pages** trip the fact-preservation guardrail. None are real case-fact losses:

| Page | Flag | Category |
|---|---|---|
| 8 | date `8-5-74` | FD-65 form revision date (boilerplate) |
| 10 | date `9-30-74` | FD-320 form revision date (boilerplate) |
| 25 | date `Aug. 16,\n1975` | Guardrail whitespace bug — fact IS in clean_text |
| 41 | dollars `$4`, `$8` | OCR garbage from footer junk, not real money |
| 51 | date `6-25-76` | FD-479 form revision date (boilerplate) |

Three categories of remaining flag noise — none are real losses. Acceptable for Phase 1.

---

## Deferred / Known Limitations

| Item | Reason | When to revisit |
|---|---|---|
| OCR character substitutions (`rn→m`, `0→O`) | Dangerous without dictionary-aware swap | Only if retrieval fails on a specific OCR error class |
| OpenCV image preprocessing (deskew, denoise, threshold) | 2× OCR time; new tool to learn; already have 50+ usable pages | Only if retrieval fails on specific low-confidence pages |
| FD-350 metadata extraction quality | OCR mangles field labels too badly to read reliably on some pages | Phase 3 or when needed for filtered retrieval |
| 4-750 deletion-reference extraction | OCR mangles reference like `Ou 6% -6¢9S-7p. |` | Phase 3 if useful for traceability |
| Guardrail whitespace false-flag on page 25 | Cosmetic — fact is preserved | Optional polish |
| Dollar regex false-positives on OCR garbage | Cosmetic — over-flags, no data loss | Optional polish |
| Statistical boilerplate detection | Template-aware approach makes it redundant for FBI docs | If we ingest non-FBI doc types later |

---

## What's Next

**Roadmap Phase 1, Day 3 onward:**

1. **`chunk.py`** — split each non-empty `clean_text` into ~500-token chunks with 100-token overlap. Each chunk carries metadata: `page_no`, `template`, `source_file`, plus any `extracted_metadata` from the source page (newspaper, date, etc.).
2. **Pinecone setup** — create index with integrated embedding (llama-text-embed-v2), upsert chunks with metadata.
3. **`query.py`** — search → build prompt → call Claude → print cited answer.

Phase 1 milestone target: *"Type a Bundy question, get a cited answer."*

---

## Files Produced

```
C:\Learning\Case_File_AI\
├── probe.py                        # PDF triage
├── tools_check.py                  # toolchain verification
├── ocr.py                          # bulk OCR
├── score_pages.py                  # threshold tuning
├── clean_pages.py                  # template-aware cleaner
├── inspect_unknowns.py             # untemplated page inspector
├── inspect_flags.py                # guardrail flag inspector
├── requirements.txt                # Python deps (Tesseract/Poppler still OS-level)
├── CaseFile-AI-Production-Roadmap.md
├── CaseFile-AI-Cleaner-Spec.md
├── DATA_PREP_LOG.md                # this file
└── data/
    ├── raw/
    │   └── bundy-part-01.pdf
    └── ocr/
        └── bundy-part-01/
            ├── pages.jsonl         # 85 rows, full per-page schema
            ├── page_001.txt..page_085.txt   # raw OCR
            └── clean/
                └── page_NNN.txt    # 50 cleaned text files
```
