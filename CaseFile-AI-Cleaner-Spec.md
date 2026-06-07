# SPEC: clean_pages.py — Template-Aware OCR Cleaner

> Sits between OCR output (`pages.jsonl`) and chunking. Turns accurate-but-noisy OCR text into chunk-ready text by removing FBI form boilerplate and labeling redactions — without ever silently deleting case facts. Rules are keyed on FBI form type, so they carry unchanged to Bundy Parts 02/03 and other Vault files.

---

## Design decisions (settled — do not re-litigate)

- **Template detection is the foundation**, not statistical line-frequency. Understanding *what a page is* (which FBI form) enables structure-aware decisions a frequency count cannot make. The statistical-repetition approach is dropped — redundant once template detection exists.
- **No OCR character substitutions** (`rn→m`, `0→O`, `l→1`). Too dangerous applied naively ("torn"→"tom", "1977" corrupted). Deferred until a measured retrieval failure traces to this specific error class.
- **Extract-then-strip** for metadata-bearing boilerplate — don't delete what should become structured metadata.
- **Fact-preservation guardrail** makes aggressive stripping safe — self-tuning per document, no hardcoded values.
- **Conservative default** — never strip aggressively on a page that couldn't be classified.

---

## Contract / scope

- **Input:** `data/ocr/bundy-part-01/pages.jsonl`, after `score_pages.py --threshold 60` has written a `bucket` field. The cleaner only processes pages where `bucket == "clean"`. (Scorer decides what's worth cleaning; cleaner only cleans. Keep the contract single-purpose.)
- **Exception:** the `4-750` DELETED-PAGE routing (Step 2) runs on clean-bucket pages regardless — those sheets score high and WILL land in the clean bucket, and must be routed out before cleaning touches them.
- **Never overwrite `raw_text`.** Write `clean_text` as a new field. Also write per-page `data/ocr/bundy-part-01/clean/page_NNN.txt` for human diffing.

---

## Pipeline — exact execution order

Order is load-bearing. Do not reorder (e.g. normalization must run last, or hyphen-rejoin glues boilerplate onto body text).

### Step 1 — Detect template (Layer 1)

Match the FBI form number near the top of the page, **fuzzily** (OCR mangles it — "FD-36 Rev. 7-27-76" came through as "FD-36 Mev. 7-27-76"). Regex like `FD-?\d{2,3}` or `4-?750`.

Recognize:
- `FD-36` → teletype
- `FD-350` → newspaper clipping
- `4-750` → FOIPA DELETED-PAGE sheet
- other `FD-xxx` (FD-65, FD-320, FD-192, FD-315…) → generic form
- no detectable form number (e.g. continuation pages "PAGE TWO DN 88-10975") → `unknown`

If `unknown` or detection is uncertain → set `template_confidence = low`, apply ONLY Step 5 general rules. Never run a structural cut on an unclassified page.

### Step 2 — Route out DELETED-PAGE sheets

If template is `4-750`: do NOT clean or chunk. Extract the reference number (e.g. `SU 88-6895-7 p.1`) and the checked exemption codes (`b7C`, `b7D`, etc.) into metadata. Set `routed_to_missing_info = true`. Remove from the rest of the pipeline. These are deletion records, not content — cleaning them is a category error.

### Step 3 — Extract metadata BEFORE stripping (FD-350 only)

For `FD-350` clippings, pull these fields out of the footer block into structured metadata **before** any stripping (the footer must be intact to read them): newspaper name, city/state, date, headline/title, author. These map directly to the chunk metadata schema (case_name, document_type, date, section_type). This turns boilerplate into retrieval signal — do not just delete it.

### Step 4 — Template-specific structural cut

- **FD-36 teletype:** anchor on the structural `CLEAR`/`BT` markers (more robust than header regex — BT is a document convention, headers vary by era/OCR run). Keep the message body between them. Discard outside it: form header, `TRANSMIT VIA`/`PRECEDENCE`/`CLASSIFICATION` block + checkbox tokens, `SEARCHED/SERIALIZED/INDEXED/FILED` stamp, footer (`Approved`, `Transmitted`, `(Number)`, `(Time)`, `GPO :`). Keep `FM.../TO.../RE.../Date` routing lines as light provenance.
  - **Fallback:** if BT delimiters are missing or garbled, `template_confidence = low` → skip the structural cut, fall through to Step 5 general rules only. (Don't guess where the body starts.)
- **FD-350 clipping:** strip the `Mount Clipping in Space Below` header and the (already-extracted) footer block. Keep the article body.
- **Generic FD-xxx:** light strip — form-number header line and SEARCHED stamp only. Keep field labels and values (DOB, charge, bond often live here). When unsure, keep.

### Step 5 — General pattern rules (every page)

- Drop lines with letter-ratio < ~40% (kills soup like `i t t I | | o i] o o a \o ~~ Bie ae meme`).
- Drop orphan lines of 1–3 stray characters (`7 °@`, `Tr . gg`, `} a`).
- Strip control / non-printable chars (Unicode category Cc/Cf).
- Strip the SEARCHED/SERIALIZED/INDEXED/FILED stamp wherever it appears (not just FD-36).
- **Label redactions inline.** Per-token heuristic: a token is a redaction artifact if `len ≥ 3` AND (contains `@`, `{`, `}`, or `::`) OR (letter-ratio < 0.4). Replace with `[REDACTED]`. Example, page 4 `SA@QEEEEE GBD 0::2002 ISSUED` → `SA [REDACTED] GBD [REDACTED] ISSUED` (`GBD`/`ISSUED` clean, left alone). Spot-check false-fire rate on 2–3 pages and tune.

### Step 6 — Normalize text (LAST)

Collapse whitespace runs and blank lines, strip trailing whitespace, rejoin end-of-line hyphenation (`(\w+)-\n(\w+)` → `\1\2`), force UTF-8. Last on purpose — must run after boilerplate removal.

### Step 7 — Fact-preservation guardrail (safety net)

- At the START of processing each page, extract from `raw_text` the set of: dates, dollar amounts, case/file numbers, and proper names. (Extract per-page from raw — do NOT hardcode `88-6895`; that breaks on Part 02.)
- After cleaning, verify that exact set still appears in `clean_text`.
- If any fact was lost, set `cleaning_flags` with the missing items and DO NOT silently proceed — these are pages where a strip rule was too aggressive. Surface them for review.

---

## Output schema (per page)

```json
{
  "page_no": 4,
  "raw_text": "...",                  // unchanged
  "clean_text": "...",                // NEW — what chunking reads
  "template": "FD-36",                // NEW
  "template_confidence": "high",      // NEW — high | low
  "extracted_metadata": {},           // NEW — FD-350 newspaper/date/headline/etc.
  "redactions_inserted": 3,           // NEW — diagnostic
  "boilerplate_lines_removed": 12,    // NEW — diagnostic
  "routed_to_missing_info": false,    // NEW — true for 4-750 sheets
  "cleaning_flags": []                // NEW — fact-preservation warnings
}
```

Plus `clean/page_NNN.txt` per page for eyeballing.

---

## Verification (before running on full keeper set)

1. Smoke-test on 2–3 pages of EACH template type: a teletype (e.g. p4), a clipping (e.g. p21 or p47), a 4-750 sheet (e.g. p16 or p30), a generic form (e.g. p8). Confirm furniture gone, facts intact.
2. Print before/after for those samples for human eyeball.
3. Report: pages per detected template; count routed to missing-info; **any page that tripped the fact-preservation guardrail** (read these — they're the cleaner being too aggressive); redaction false-fire spot-check result.
4. Only after smoke test passes, run on the full clean-bucket set.

---

## Watch-outs

- **Don't let template detection miss mangled form numbers** — if the per-template counts look too low (e.g. "3 teletypes" when there are clearly many), the fuzzy match is too strict.
- **Redaction heuristic leans aggressive** — it will catch more real redactions at the cost of some false fires. Acceptable if the spot-check confirms the rate is low; tighten the symbol set if it eats real tokens.
- **Generic-form light strip stays light** — these forms hold real field data (DOB, bond). Bias toward keeping.
