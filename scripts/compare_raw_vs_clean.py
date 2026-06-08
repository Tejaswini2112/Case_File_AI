"""
One-off audit: for every page in the 'clean' bucket, compare raw_text vs
clean_text and surface two failure modes for human review:

  (A) Potentially LOST case facts — raw lines that look fact-bearing
      (names, dates, places, identifiers, body prose) but are not represented
      in clean_text. The cleaning_flags field on pages.jsonl already catches
      the structured fact patterns; this layer is broader and looks for
      proper-noun / sentence-shaped content too.

  (B) RESIDUAL noise still in clean_text — fragments that have no value
      (OCR slop, form furniture, repeated-char runs, single-glyph orphans
      that slipped through).

Outputs a Markdown report at:
    data/ocr/<name>/raw_vs_clean_audit.md
"""

import json
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

JSONL_PATH = Path(r"c:\Learning\Case_File_AI\data\ocr\bundy-part-01\pages.jsonl")
REPORT_PATH = JSONL_PATH.parent / "raw_vs_clean_audit.md"

# ----------------------------------------------------------------------------
# Heuristics for "this line might carry real case info"
# ----------------------------------------------------------------------------

# Already covered by cleaning_flags, but re-check here so the audit is self-contained
DATE_RE = re.compile(
    r"\b(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\.?\s+\d{1,2},?\s+\d{4}\b"
    r"|\b\d{1,2}/\d{1,2}/\d{2,4}\b"
    r"|\b\d{1,2}-\d{1,2}-\d{2,4}\b",
    re.IGNORECASE,
)
DOLLAR_RE = re.compile(r"\$\s*\d[\d,]*(?:\.\d+)?")
CASE_NUM_RE = re.compile(r"\b\d{2,3}-\d{4,6}(?:-\d+)?\b")

# Broader fact-like signals
SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
PHONE_RE = re.compile(r"\b\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b")
HEIGHT_WEIGHT_RE = re.compile(r"\b\d['’]\s*\d{0,2}\"?\b|\b\d{2,3}\s*LBS?\b", re.IGNORECASE)
ROUTING_RE = re.compile(r"\b(?:FM|TO)\s+[A-Z][A-Z\s]{2,}\b")  # FM SALT LAKE CITY etc.
PLACE_HINT_RE = re.compile(
    r"\b(?:UTAH|COLORADO|WASHINGTON|VERMONT|ASPEN|SALT\s+LAKE|SEATTLE|DENVER|"
    r"BURLINGTON|GLENWOOD|TALLAHASSEE|FLORIDA|PENSACOLA|MIAMI)\b",
    re.IGNORECASE,
)
NAME_HINT_RE = re.compile(r"\bBUNDY\b|\bTHEODORE\b|\bTED\b", re.IGNORECASE)
TITLE_HINT_RE = re.compile(
    r"\b(?:AUSA|SA|SAC|USA|FBI|U\.\s*S\.\s*MAGISTRATE|"
    r"COUNTY\s+ATTORNEY|JUDGE|DETECTIVE|SHERIFF|MARSHAL)\b",
    re.IGNORECASE,
)
LEGAL_HINT_RE = re.compile(
    r"\b(?:TITLE\s+\d+|U\.\s*S\.\s*CODE|SECTION\s+\d+|VIOLATION|WARRANT|COMPLAINT|"
    r"INDICTMENT|FUGITIVE|UFAC|UNLAWFUL\s+FLIGHT|ESCAPE|ARREST|CONVICTION)\b",
    re.IGNORECASE,
)
PROPER_NOUN_RUN_RE = re.compile(r"\b(?:[A-Z][a-z]+\s+){2,}[A-Z][a-z]+\b")  # "James W McConkie"

FACT_REGEXES = [
    ("date", DATE_RE),
    ("dollar", DOLLAR_RE),
    ("case_number", CASE_NUM_RE),
    ("ssn", SSN_RE),
    ("phone", PHONE_RE),
    ("height_weight", HEIGHT_WEIGHT_RE),
    ("routing", ROUTING_RE),
    ("place", PLACE_HINT_RE),
    ("name", NAME_HINT_RE),
    ("title", TITLE_HINT_RE),
    ("legal", LEGAL_HINT_RE),
    ("proper_noun_run", PROPER_NOUN_RUN_RE),
]


def fact_signals(line: str) -> list[str]:
    """Return list of fact-type tags present in a line."""
    return [tag for tag, rx in FACT_REGEXES if rx.search(line)]


# ----------------------------------------------------------------------------
# Heuristics for "this fragment of clean_text is residual noise"
# ----------------------------------------------------------------------------

REPEAT_CHAR_RUN_RE = re.compile(r"(.)\1{4,}|(?:\b\w{1,2}\b\s+){4,}")
# things like "ee ee ee ee" or "—— —— ——"
SHORT_GIBBERISH_RE = re.compile(r"^[^A-Za-z0-9]{2,}$")  # only symbols
LOW_INFO_TOKEN_RE = re.compile(r"^[a-z]{1,2}$|^[^\w]+$")

FORM_FURNITURE_LEFTOVERS = re.compile(
    r"\b(?:FD-?\s*36|FD-?\s*350|TRANSMIT\s+VIA|PRECEDENCE|CLASSIFICATION|"
    r"Mev\.?\s*\d|Rev\.?\s*\d|OEFTO|UEFTO|GPO\s*:|\(Number\)|\(Time\)|"
    r"Mount\s+Clipping)\b",
    re.IGNORECASE,
)


def residual_noise_findings(clean_text: str) -> list[str]:
    """Return list of human-readable noise descriptions found in clean_text."""
    findings: list[str] = []
    if not clean_text:
        return findings

    # 1. form furniture that survived
    for m in FORM_FURNITURE_LEFTOVERS.finditer(clean_text):
        findings.append(f"form-furniture leftover: '{m.group(0)}'")

    # 2. long repeated-char run (e.g. 'eeeeeeee', '________', '~~~~~')
    for m in re.finditer(r"(.)\1{5,}", clean_text):
        findings.append(f"repeated-char run: {m.group(0)!r}")

    # 3. long runs of tiny words ("ee ee ee ee ee")
    for m in re.finditer(r"(?:\b\w{1,2}\b[\s,]+){5,}", clean_text):
        snippet = m.group(0).strip()
        if len(snippet) > 12:
            findings.append(f"tiny-word run: '{snippet[:60]}{'...' if len(snippet) > 60 else ''}'")

    # 4. stretches with very low letter ratio (50+ chars where < 35% are letters)
    for chunk in re.split(r"(?<=\.)\s+", clean_text):
        if len(chunk) >= 50:
            letter_ratio = sum(c.isalpha() for c in chunk) / len(chunk)
            if letter_ratio < 0.35:
                findings.append(
                    f"low-letter-ratio chunk ({letter_ratio:.0%}): '{chunk[:80]}...'"
                )

    # 5. isolated junk tokens like "i", "a", "o", "}", "~"
    tokens = clean_text.split()
    junk = [t for t in tokens if len(t) == 1 and not t.isalnum()]
    if len(junk) >= 5:
        findings.append(f"{len(junk)} stray single-symbol tokens: {' '.join(junk[:8])}")

    return findings


# ----------------------------------------------------------------------------
# Per-page diff: which raw lines are missing from clean_text?
# ----------------------------------------------------------------------------


def normalize_for_match(s: str) -> str:
    """Lowercase + strip non-alphanumerics so we can substring-match across cleaner."""
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def line_present_in_clean(raw_line: str, clean_norm: str) -> bool:
    """
    Is the bulk of raw_line still in clean_text? We use a token-overlap test:
    if >=70% of the raw line's word tokens (length >= 3) appear in clean_text,
    we consider it preserved.
    """
    raw_norm = normalize_for_match(raw_line)
    tokens = [t for t in raw_norm.split() if len(t) >= 3]
    if not tokens:
        # No anchorable tokens — too short to evaluate. Treat as preserved.
        return True
    present = sum(1 for t in tokens if t in clean_norm)
    return present / len(tokens) >= 0.7


def find_lost_fact_lines(raw: str, clean: str) -> list[tuple[str, list[str]]]:
    """
    Return list of (raw_line, [fact_tags]) for raw lines that look fact-bearing
    but are not present in clean_text.
    """
    clean_norm = normalize_for_match(clean)
    lost: list[tuple[str, list[str]]] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if len(stripped) < 4:
            continue
        tags = fact_signals(stripped)
        if not tags:
            continue
        if not line_present_in_clean(stripped, clean_norm):
            lost.append((stripped, tags))
    return lost


# ----------------------------------------------------------------------------
# Driver
# ----------------------------------------------------------------------------


def main() -> None:
    pages = [json.loads(line) for line in JSONL_PATH.open(encoding="utf-8") if line.strip()]
    clean_pages = [
        p for p in pages
        if p.get("bucket") == "clean"
        and not p.get("routed_to_missing_info")
        and p.get("clean_text")
    ]

    rows: list[dict] = []
    for p in clean_pages:
        raw = p["raw_text"]
        clean = p["clean_text"]
        lost = find_lost_fact_lines(raw, clean)
        noise = residual_noise_findings(clean)
        rows.append({
            "page_no": p["page_no"],
            "template": p.get("template"),
            "confidence": p.get("template_confidence"),
            "raw_chars": len(raw),
            "clean_chars": len(clean),
            "reduction_pct": (1 - len(clean) / max(len(raw), 1)) * 100,
            "cleaning_flags": p.get("cleaning_flags", []),
            "lost_fact_lines": lost,
            "residual_noise": noise,
        })

    # ---- write report ----
    out = []
    out.append("# Raw vs Clean audit — bundy-part-01\n")
    out.append(f"- Total clean-bucket pages with clean_text: **{len(clean_pages)}**")
    pages_with_lost = sum(1 for r in rows if r["lost_fact_lines"])
    pages_with_noise = sum(1 for r in rows if r["residual_noise"])
    pages_with_existing_flags = sum(1 for r in rows if r["cleaning_flags"])
    out.append(f"- Pages where existing cleaning_flags already fired: **{pages_with_existing_flags}**")
    out.append(f"- Pages where audit found potentially lost fact-bearing lines: **{pages_with_lost}**")
    out.append(f"- Pages where audit found residual noise in clean_text: **{pages_with_noise}**\n")

    # Summary table
    out.append("## Per-page summary\n")
    out.append("| Page | Template | Conf | Raw→Clean chars | % reduced | Existing flags | Lost-line hits | Residual noise hits |")
    out.append("|------|----------|------|-----------------|-----------|----------------|-----------------|----------------------|")
    for r in rows:
        out.append(
            f"| {r['page_no']:>3} | {r['template'] or '-':<10} | {r['confidence'] or '-':<4} "
            f"| {r['raw_chars']}→{r['clean_chars']} | {r['reduction_pct']:.0f}% "
            f"| {len(r['cleaning_flags'])} "
            f"| {len(r['lost_fact_lines'])} "
            f"| {len(r['residual_noise'])} |"
        )
    out.append("")

    # Detail sections
    out.append("## Detail — pages with potentially lost fact-bearing lines\n")
    for r in rows:
        if not r["lost_fact_lines"]:
            continue
        out.append(f"### Page {r['page_no']} (template={r['template']})")
        for line, tags in r["lost_fact_lines"]:
            out.append(f"- **tags={','.join(tags)}** — `{line}`")
        out.append("")

    out.append("## Detail — pages with residual noise in clean_text\n")
    for r in rows:
        if not r["residual_noise"]:
            continue
        out.append(f"### Page {r['page_no']} (template={r['template']})")
        for n in r["residual_noise"]:
            out.append(f"- {n}")
        out.append("")

    out.append("## Detail — pages where the existing cleaning_flags already fired\n")
    for r in rows:
        if not r["cleaning_flags"]:
            continue
        out.append(f"- Page {r['page_no']}: {json.dumps(r['cleaning_flags'], ensure_ascii=False)}")
    out.append("")

    REPORT_PATH.write_text("\n".join(out), encoding="utf-8")
    print(f"Wrote {REPORT_PATH}")
    print(f"  pages audited: {len(clean_pages)}")
    print(f"  with lost-fact-line hits:   {pages_with_lost}")
    print(f"  with residual-noise hits:   {pages_with_noise}")
    print(f"  with pre-existing flags:    {pages_with_existing_flags}")


if __name__ == "__main__":
    main()
