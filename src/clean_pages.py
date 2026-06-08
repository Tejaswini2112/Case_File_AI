"""
Step 3 — Template-aware OCR cleaner.

Reads:  data/ocr/<name>/pages.jsonl   (must have bucket field — run score_pages.py first)
Writes: data/ocr/<name>/pages.jsonl   (adds clean_text + diagnostic fields)
        data/ocr/<name>/clean/page_NNN.txt   (per page, for human diffing)

Only processes pages where bucket == "clean". 4-750 DELETED-PAGE sheets, even
when they land in the clean bucket, are routed to missing-info instead of cleaned.

See docs/cleaner-spec.md for the design.

Usage (run from project root):
    python src/clean_pages.py data/ocr/bundy-part-01/pages.jsonl
    python src/clean_pages.py data/ocr/bundy-part-01/pages.jsonl --detect-only
    python src/clean_pages.py data/ocr/bundy-part-01/pages.jsonl --pages 4,15,47
"""

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

# ============================================================================
# Template detection (Layer 1)
# ============================================================================

# Change #3: allow period or dash as separator (OCR sometimes reads `-` as `.`).
# Change #4: allow 1-3 digit form numbers (not just 2-3) so FD-4 is detected.
# Change #5: add teletype-continuation detection BEFORE the generic FD-xxx
# fallback so continuation pages don't accidentally match the generic pattern.
PAGE_CONTINUATION_RE = re.compile(
    r"\bPAGE\s+(?:TWO|THREE|FOUR|FIVE|SIX|SEVEN|EIGHT|NINE|TEN|"
    r"ELEVEN|TWELVE|THIRTEEN|FOURTEEN|FIFTEEN|SIXTEEN)\b",
    re.IGNORECASE,
)

# Order matters: specific patterns before the generic FD-xxx fallback.
#
# PAGE_CONTINUATION_RE is FIRST among the FD-shaped patterns: a teletype
# continuation page (page 5, page 27, etc.) has the FD-36 form-ID line at
# the top AND a "PAGE TWO/THREE" header below it. If FD-36 matched first
# we'd route to handle_fd36, which on a single BT marker assumes the BT is
# the body-START. On continuation pages the BT is the body-END — body is
# above it. The wrong handler would delete the body. Detect cont first.
TEMPLATE_PATTERNS = [
    (re.compile(r"\b4[-.]?\s*750\b"), "4-750"),
    (PAGE_CONTINUATION_RE, "FD-36-cont"),
    (re.compile(r"\bFD[-.]?\s*36\b(?!\d)"), "FD-36"),
    (re.compile(r"\bFD[-.]?\s*350\b"), "FD-350"),
    (re.compile(r"\bFD[-.]?\s*\d{1,3}\b"), "FD-xxx"),
]


def detect_template(text: str) -> str:
    """Look at the first 20 lines for an FBI form number. Returns template name or 'unknown'.

    20 lines (not 12) so we catch "PAGE TWO" on FD-36 continuation pages — it
    sits below ~12 lines of form furniture (FD-36 form-ID, transmit checkbox
    column, classification, dashed separator) before the body header.
    """
    head = "\n".join(text.splitlines()[:20])
    for pattern, name in TEMPLATE_PATTERNS:
        if pattern.search(head):
            return name
    return "unknown"


# ============================================================================
# Fact extraction (for the preservation guardrail)
# ============================================================================

DATE_PATTERNS = [
    # "JUNE 9, 1977" or "JUNE 9 1977"
    re.compile(
        r"\b(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\.?\s+\d{1,2},?\s+\d{4}\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b"),       # 6/9/77 or 6/9/1977
    re.compile(r"\b\d{1,2}-\d{1,2}-\d{2,4}\b"),       # 6-9-77
    re.compile(r"\b\d{4}-\d{1,2}-\d{1,2}\b"),         # 1977-06-09
]
DOLLAR_PATTERN = re.compile(r"\$\s*\d[\d,]*(?:\.\d+)?")
CASE_NUMBER_PATTERN = re.compile(r"\b\d{2,3}-\d{4,6}(?:-\d+)?\b")

# Form revision dates ("FD-36 (Rev. 7-27-76)", "Mev. 7-27-76", "Rew. 11-11-75",
# etc.) — form-ID metadata, NOT case facts. Used by extract_facts (to keep the
# guardrail from tripping on intentionally-stripped form metadata) and by
# line_contains_fact (to keep the form-ID line from being protected by its
# own rev date).
FORM_REV_DATE_RE = re.compile(
    r"\(?\s*(?:Rev|Mev|Rew|Rey|Hee|fev|Gev|hev|Lee)\.?,?\s*"
    r"\d{1,2}[-./]\d{1,2}[-./]\d{2,4}\b\)?",
    re.IGNORECASE,
)

# Change #2: also catch parenthesized FOIA exemption forms like (b)(7)(A), (b)(1).
EXEMPTION_PATTERN = re.compile(
    r"\bb[1-9][A-E]?\b"                                  # bare:        b7C, b3
    r"|"
    r"\(b\)\s*\(\s*[1-9]\s*\)(?:\s*\(\s*[A-E]\s*\))?",   # parenthesized: (b)(7)(A), (b)(1)
    re.IGNORECASE,
)


def normalize_exemption(s: str) -> str:
    """Strip parens/whitespace so '(b)(7)(A)' and 'b7A' dedupe to the same key."""
    return re.sub(r"[()\s]", "", s).lower()


def extract_facts(text: str) -> dict[str, set[str]]:
    """Pull dates, dollar amounts, and case file numbers.

    Form-revision dates ("FD-36 (Rev. 7-27-76)") and GPO printing codes
    ("GPO : 1977 © - 225-5358") are NOT case facts. Mask them out of the
    text before extracting, so the guardrail doesn't trip when the cleaner
    correctly strips them from clean_text.
    """
    masked = FORM_REV_DATE_RE.sub(" ", text)
    masked = re.sub(r"\bGPO\b\s*[:.\s][^\n]*", " ", masked, flags=re.IGNORECASE)

    dates: set[str] = set()
    for p in DATE_PATTERNS:
        dates.update(m.group(0) for m in p.finditer(masked))
    return {
        "dates": dates,
        "dollars": {m.group(0) for m in DOLLAR_PATTERN.finditer(masked)},
        "case_numbers": {m.group(0) for m in CASE_NUMBER_PATTERN.finditer(masked)},
    }


# ============================================================================
# Token-level: redaction labeling
# ============================================================================

# Symbols that strongly suggest OCR-over-a-black-bar artifact.
REDACTION_SYMBOL_RE = re.compile(r"[@{}]|::")


def is_redaction_token(token: str) -> bool:
    """
    True if a token is likely an OCR'd redaction artifact.

    Rule (refinement of spec line 63 to fix numeric-token bug):
        len >= 3 AND (
            contains @, {, }, or ::    OR
            (has at least one letter AND non-alphanumeric-symbol ratio > 0.3)
        )

    Fact-bearing tokens are protected: even if a token has stray punctuation
    from OCR (e.g. "(88-6895}—." where the closing paren was read as "}"),
    we won't redact it because it carries real case data. Same principle as
    line_contains_fact for the line-level filters. This catches the
    page-46-style loss where a case number was nuked along with its artifact.
    """
    if len(token) < 3:
        return False
    if line_contains_fact(token):
        return False
    if REDACTION_SYMBOL_RE.search(token):
        return True
    has_letter = any(c.isalpha() for c in token)
    if not has_letter:
        return False
    symbol_count = sum(1 for c in token if not c.isalnum())
    return (symbol_count / len(token)) > 0.3


def label_redactions(text: str) -> tuple[str, int]:
    """Replace redaction-artifact tokens with [REDACTED]. Collapse runs."""
    out: list[str] = []
    inserted = 0
    for token in text.split():
        if is_redaction_token(token):
            if out and out[-1] == "[REDACTED]":
                continue  # collapse consecutive
            out.append("[REDACTED]")
            inserted += 1
        else:
            out.append(token)
    return " ".join(out), inserted


# ============================================================================
# Line-level filters
# ============================================================================

SHORT_KEEPLIST = {"I", "A", "i", "a", "FM", "TO", "RE", "BT", "OK", "USA", "FBI", "AKA", "SA"}


def ascii_letter_ratio(s: str) -> float:
    if not s:
        return 1.0
    return sum(c.isalpha() and c.isascii() for c in s) / len(s)


def line_contains_fact(line: str) -> bool:
    """True if the line carries a date, dollar amount, or case file number.

    Excludes two form-furniture cases that would otherwise be falsely protected:
      - GPO printing-code lines ("GPO : 1977 © - 225-5358") — the 225-5358
        catalog code matches CASE_NUMBER_PATTERN but isn't case data.
      - Form revision dates ("FD-36 (Rev. 7-27-76)") — masked before the check.
    """
    if re.search(r"\bGPO\b", line, re.IGNORECASE):
        return False
    masked = FORM_REV_DATE_RE.sub(" ", line)
    if DOLLAR_PATTERN.search(masked) or CASE_NUMBER_PATTERN.search(masked):
        return True
    return any(p.search(masked) for p in DATE_PATTERNS)


_VOWEL_ONLY_RE = re.compile(r"^[aeiouAEIOU]+$")
_SHORT_CAPS_RE = re.compile(r"^[A-Z]{2,3}$")


def is_horizontal_rule_artifact(line: str) -> bool:
    """
    Detect the horizontal dashed separator at the top of teletype body
    sections that OCR renders as a run of short vowel-only fragments —
    e.g. 'ae meme ieee ree eee eee ee ee eee eee eee ee eee ee ee'.

    Signal: 6+ tokens, every token ≤4 chars, and at least half are pure
    vowel sequences ('ee', 'eee', 'ae', 'ieee', etc.).
    """
    tokens = line.strip().split()
    if len(tokens) < 6:
        return False
    if not all(len(t) <= 4 for t in tokens):
        return False
    vowel_only = sum(1 for t in tokens if _VOWEL_ONLY_RE.fullmatch(t))
    return vowel_only / len(tokens) >= 0.5


def is_bureau_code_column(line: str) -> bool:
    """
    Detect the FBI bureau-code column on teletype headers — a row of 2-3
    letter all-caps abbreviations like 'RR SU AT NK NY PH SF SE DE DN'.
    These are routing-priority codes per addressee office, not body content.

    Signal: 6+ tokens, ≥70% are 2-3 letter all-caps tokens.
    """
    tokens = line.strip().split()
    if len(tokens) < 6:
        return False
    short_caps = sum(1 for t in tokens if _SHORT_CAPS_RE.fullmatch(t))
    return short_caps / len(tokens) >= 0.7


def classify_drop(line: str) -> str | None:
    """
    Decide whether to drop a line. Returns:
        'orphan'        — short stray fragment (1-3 chars with symbols, or meaningless)
        'soup'          — long-enough line whose letter ratio is below 40%
        'rule-artifact' — OCR'd horizontal rule rendered as vowel-soup
        'bureau-codes'  — FBI routing-priority code column
        None            — keep
    """
    stripped = line.strip()
    if not stripped:
        return None  # blank lines handled by normalize

    # PROTECT lines carrying structured case facts.
    if line_contains_fact(stripped):
        return None

    if len(stripped) <= 3:
        if stripped in SHORT_KEEPLIST:
            return None
        return "orphan"

    if is_horizontal_rule_artifact(stripped):
        return "rule-artifact"

    if is_bureau_code_column(stripped):
        return "bureau-codes"

    if ascii_letter_ratio(stripped) < 0.40:
        return "soup"

    return None


# Stamps and form furniture that can appear on many template types.
#
# The 4-corner routing stamp ("SEARCHED___ INDEXED___ SERIALIZED___ FILED___")
# is form furniture. But "FILED" and "SEARCHED" are also common verbs in body
# prose ("Complaint filed before Magistrate Alsup", "lawmen searched the area").
# We discriminate by requiring a *stamp-shaped* context: multiple stamp words
# together, an underscore-blank next to the word, or a near-empty line that's
# just the keyword surrounded by punctuation.
STAMP_KEYWORDS = r"SEARCHED|SERIALIZED|SERIALIZEO|INDEXED|FILED"
STAMP_KEYWORD_RE = re.compile(rf"\b(?:{STAMP_KEYWORDS})\b", re.IGNORECASE)
# Keyword followed by underscores (with or without intervening whitespace).
# Note: `\b` after the keyword doesn't fire against `_`, so we don't use it here.
STAMP_WITH_BLANK_RE = re.compile(rf"\b(?:{STAMP_KEYWORDS})[_\s]*_+", re.IGNORECASE)


def is_stamp_line(line: str) -> bool:
    """True if a line looks like the FBI routing stamp, not body prose."""
    stripped = line.strip()
    if not stripped:
        return False
    matches = STAMP_KEYWORD_RE.findall(stripped)
    if not matches:
        return False
    # 1. Two or more stamp keywords on one line — the stamp grid collapsed onto a row.
    if len(matches) >= 2:
        return True
    # 2. Stamp keyword followed by an underscore-blank (where date/initials go).
    if STAMP_WITH_BLANK_RE.search(stripped):
        return True
    # 3. Near-empty line: <=3 tokens AND every non-stamp token is non-alphabetic
    #    (punctuation, digits, OCR symbols). Body prose with "filed" always has
    #    other real words ("were formally filed", "complaint filed by ...").
    words = stripped.split()
    if len(words) <= 3:
        non_stamp = [w for w in words if not STAMP_KEYWORD_RE.fullmatch(w)]
        if all(not any(c.isalpha() for c in w) for w in non_stamp):
            return True
    return False


FD36_HEADER_RE = re.compile(
    r"^\s*(?:FD-?\s*36\b|TRANSMIT VIA|PRECEDENCE|CLASSIFICATION|"
    r"Teletype|Facsimile|Airtel|Immediate|Priority|Routine|"
    r"TOP SECRET|SECRET|CONFIDENTIAL|EFTO|\[?[Xx]\]?\s*CLEAR)",
    re.IGNORECASE,
)
FD36_FOOTER_RE = re.compile(
    r"\b(?:Approved|Transmitted|GPO\s*:)\b|\(Number\)|\(Time\)",
    re.IGNORECASE,
)


def strip_searched_stamp(text: str) -> tuple[str, int]:
    lines = text.splitlines()
    kept: list[str] = []
    removed = 0
    for line in lines:
        if is_stamp_line(line):
            removed += 1
            continue
        kept.append(line)
    return "\n".join(kept), removed


# ============================================================================
# Whole-text cleanup
# ============================================================================


def strip_control_chars(text: str) -> str:
    """Drop Unicode Cc/Cf, but keep newlines and tabs."""
    return "".join(c for c in text if c in "\n\t" or unicodedata.category(c)[0] != "C")


def normalize_text(text: str) -> str:
    """Whitespace + hyphenation cleanup. Run LAST."""
    text = re.sub(r"(\w+)-\n(\w+)", r"\1\2", text)              # rejoin EOL hyphens
    text = re.sub(r"[ \t]+", " ", text)                          # collapse runs of spaces/tabs
    text = "\n".join(line.rstrip() for line in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text)                       # collapse blank-line runs
    return text.strip()


# ============================================================================
# Template handlers (Layer 2)
# ============================================================================


def handle_4750(raw: str) -> dict:
    """Route a DELETED-PAGE sheet to missing-info. Extract reference + exemptions."""
    ref_match = re.search(
        r"\b[A-Z]{2}\s*\d{2,3}-?\d{2,6}(?:-\d+)?(?:\s*p\.?\s*\d+)?",
        raw,
    )
    exemptions = sorted({normalize_exemption(m.group(0)) for m in EXEMPTION_PATTERN.finditer(raw)})
    return {
        "deletion_reference": ref_match.group(0) if ref_match else None,
        "exemptions_cited": exemptions,
    }


# Change #6 — FD-36 fallback: named boilerplate blocks to strip when BT markers
# are missing/garbled. Each pattern targets one well-defined block of form
# furniture. No generic letter-ratio cuts at this layer; the safety net
# (fact-preservation guardrail) catches any over-strip.
FD36_FURNITURE_PATTERNS = [
    # The FD-36 form-ID line itself, e.g. "FD-36 (Rev. 7-27-76)" / "FD-36 Mev. 7-27-76"
    re.compile(r"^.*\bFD[-.]?\s*36\b.*$", re.IGNORECASE),
    # "TRANSMIT VIA: ... PRECEDENCE: ... CLASSIFICATION:" header column line
    re.compile(r"^.*\bTRANSMIT\s+VIA\b.*$", re.IGNORECASE),
    re.compile(r"^.*\bPRECEDENCE\b.*\bCLASSIFICATION\b.*$", re.IGNORECASE),
    # Classification checkbox lines (the column of Teletype/Facsimile/Airtel/etc.)
    # NOTE: dropped "Immediate", "Priority", "Routine", and standalone "SECRET"
    # — these keywords also appear in legitimate routing lines (e.g.
    # "TO DENVER ROUTINE") which the spec says to keep as light provenance.
    # EFTO appears with various OCR'd prefixes: OEFTO, UEFTO, CIEFTO, OUEF TO
    # (the space variant happens when OCR splits the letters).
    re.compile(
        r"^.*\b(?:Teletype|Facsimile|Airtel|TOP\s+SECRET|CONFIDENTIAL"
        r"|[A-Z]{0,3}EFTO|[A-Z]?UEF\s+TO)\b.*$",
        re.IGNORECASE,
    ),
    # FBI logo-box noise at the top of FD-36 forms: a line of short fragments
    # ending in "FBI" with garbage tokens around it, e.g. ". . Cog OMe FBI \"".
    # Anchored to ≤6 tokens of ≤4 chars so it doesn't catch body prose like
    # "AUSA J. McConkie FBI Salt Lake City advised...".
    re.compile(
        r"^\s*[\W]*(?:\s*\b[A-Za-z]{1,4}\b\s*[\W]*){0,5}\bFBI\b[\W]*$",
        re.IGNORECASE,
    ),
    # Checkbox-shaped "[x] CLEAR" header — narrowly anchored so we don't strip
    # the structural ": CLEAR" or "2: CLEAR" markers that indicate body start.
    re.compile(r"^\s*[\[(].{0,3}[\])]\s*CLEAR\b.*$", re.IGNORECASE),
    # GPO printing code at footer
    re.compile(r"^.*\bGPO\b\s*[:.\s].*$", re.IGNORECASE),
    # Transmission footer labels: "(Number) (Time)", "Transmitted ___", "Approved ___"
    re.compile(r"^.*\(\s*Number\s*\).*\(\s*Time\s*\).*$", re.IGNORECASE),
    re.compile(r"^.*\bTransmitted\b\s*_+.*$", re.IGNORECASE),
    re.compile(r"^.*\bApproved\b\s*_+.*$", re.IGNORECASE),
]


def strip_fd36_furniture(text: str) -> str:
    """
    Pattern-strip the named FD-36 furniture blocks. Used only as a fallback.

    Fact-bearing lines are protected: same principle as the soup/orphan filter.
    A line like "Re Salt Lake City teletype dated June 10, 1977" matches our
    Teletype keyword but is body prose carrying a real date — keep it.
    """
    lines = text.splitlines()
    kept = []
    for line in lines:
        if line_contains_fact(line):
            kept.append(line)
            continue
        if any(p.search(line) for p in FD36_FURNITURE_PATTERNS):
            continue
        kept.append(line)
    return "\n".join(kept)


# Routing-line provenance: FM/TO/RE lines, all-caps "OFFICE ROUTINE/PRIORITY"
# destination lines, the SSAN row, and the teletype transmission-date row.
# These sit ABOVE the BT marker on FD-36 teletypes but carry real provenance
# (case#, originating office, transmission date) so we keep them on the
# smart 1-BT fallback path.
_ROUTING_PROVENANCE_RES = [
    re.compile(r"^\s*(?:FM|TO|RE)\s+[A-Z]", re.IGNORECASE),
    re.compile(r"^\s*[A-Z][A-Z\s]{2,}\s+(?:ROUTINE|PRIORITY|IMMEDIATE)\s*$"),
    re.compile(r"\bSSAN\s+\d{3}", re.IGNORECASE),
    # Teletype transmission-date line: "Date 1/3/78 =", "Date: 6/9/77"
    re.compile(r"^\s*Date\s*[:.]?\s*\d{1,2}[-./]\d{1,2}[-./]\d{2,4}\b", re.IGNORECASE),
]


def _is_routing_provenance(line: str) -> bool:
    return any(p.search(line) for p in _ROUTING_PROVENANCE_RES)


def handle_fd36(raw: str) -> tuple[str, str]:
    """
    Preferred: structural cut between the first and last BT markers.

    Single-BT fallback: when only one BT survives OCR (faded ink, marginal
    scan), treat it as a body-START marker. Everything before BT is form
    furniture — keep ONLY the routing-provenance lines (FM/TO/RE, all-caps
    routing destinations, SSAN). Everything after BT is body, but still run
    it through strip_fd36_furniture to catch the GPO footer and any other
    leakage.

    Last-resort: pattern-strip the named furniture blocks. The safety net
    catches over-stripping.

    Returns (body, confidence).
        'high'   = clean structural cut between two BT markers
        'medium' = single-BT smart fallback (anchored to one structural cue)
        'low'    = no BT markers — pattern-strip only
    """
    lines = raw.splitlines()
    bt_indices = [i for i, line in enumerate(lines) if re.search(r"\bBT\b", line)]

    if len(bt_indices) >= 2:
        body = lines[bt_indices[0] + 1 : bt_indices[-1]]
        return "\n".join(body), "high"

    if len(bt_indices) == 1:
        idx = bt_indices[0]
        provenance = [ln for ln in lines[:idx] if _is_routing_provenance(ln)]
        body_lines = lines[idx + 1 :]
        joined = "\n".join(provenance + body_lines)
        return strip_fd36_furniture(joined), "medium"

    return strip_fd36_furniture(raw), "low"


def handle_fd36_continuation(raw: str) -> tuple[str, str]:
    """
    Continuation pages ('PAGE TWO DN 88-10975 CLEAR') have no form furniture
    at the top. Body runs from the PAGE [WORD] header line (kept as light
    provenance — same convention as FM/TO routing lines on first pages)
    down to the closing BT (if present) or end of page.
    """
    lines = raw.splitlines()
    start = 0
    for i, line in enumerate(lines):
        if PAGE_CONTINUATION_RE.search(line):
            start = i  # keep the header line — it carries the case number
            break
    end = len(lines)
    for i in range(len(lines) - 1, start, -1):
        if re.search(r"\bBT\b", lines[i]):
            end = i
            break
    return "\n".join(lines[start:end]), "high"


# Fix C: anchor at line start and REQUIRE a colon (or OCR variant) after the
# label. The bare "City" alternative is gone — it was matching body prose like
# "Salt Lake City and was..." which set earliest_field too early and truncated
# the entire article. Real fields are at line start with a colon after the label.
FD350_FIELD_PATTERNS = {
    "newspaper": re.compile(
        r"^(?:Name\s+of\s+[Nn]ewspaper|Newspaper)\s*[:.;\-]\s*([^\n]+)",
        re.MULTILINE,
    ),
    "city_state": re.compile(
        r"^(?:City\s+and\s+[Ss]tate|City/State)\s*[:.;\-]\s*([^\n]+)",
        re.MULTILINE,
    ),
    "date": re.compile(r"^\s*Date\s*[:.;\-]\s*([^\n]+)", re.MULTILINE),
    "edition": re.compile(r"^\s*Edition\s*[:.;\-]\s*([^\n]+)", re.MULTILINE),
    "author": re.compile(r"^\s*Author\s*[:.;\-]\s*([^\n]+)", re.MULTILINE),
    "title": re.compile(r"^\s*Title\s*[:.;\-]\s*([^\n]+)", re.MULTILINE),
}
MOUNT_CLIPPING_RE = re.compile(r"^.*Mount\s+[Cc]lipping\s+in\s+[Ss]pace\s+[Bb]elow.*$", re.MULTILINE)


def handle_fd350(raw: str) -> tuple[str, dict, str]:
    """
    Extract metadata BEFORE stripping (the labeled footer block needs to be intact).
    Then strip the 'Mount Clipping' header and the metadata block from the body.
    """
    metadata: dict[str, str] = {}
    earliest_field = len(raw)
    for key, pattern in FD350_FIELD_PATTERNS.items():
        m = pattern.search(raw)
        if m:
            metadata[key] = m.group(1).strip()
            if m.start() < earliest_field:
                earliest_field = m.start()

    body = MOUNT_CLIPPING_RE.sub("", raw)
    # If we found any field, treat everything from that field onward as footer block.
    if earliest_field < len(raw):
        body = body[:earliest_field]

    confidence = "high" if metadata else "low"
    return body, metadata, confidence


GENERIC_FORM_HEADER_RE = re.compile(
    r"^\s*FD-?\d{2,3}\s*\(?(?:Rev|Mev)",  # "Mev" = OCR'd "Rev"
    re.IGNORECASE,
)


def handle_generic(raw: str) -> str:
    """Light strip — form-number header line only. Keep field labels and values."""
    kept = [line for line in raw.splitlines() if not GENERIC_FORM_HEADER_RE.search(line)]
    return "\n".join(kept)


# ============================================================================
# Pipeline (per page)
# ============================================================================


def clean_page(page: dict) -> dict:
    """Apply the full pipeline. Returns a new dict with cleaning fields populated."""
    raw = page["raw_text"]
    raw_facts = extract_facts(raw)

    # Step 1 — detect template
    template = detect_template(raw)
    template_confidence = "high" if template != "unknown" else "low"

    # Step 2 — route 4-750 deletion sheets out
    if template == "4-750":
        meta = handle_4750(raw)
        return {
            **page,
            "template": template,
            "template_confidence": "high",
            "extracted_metadata": meta,
            "routed_to_missing_info": True,
            "clean_text": "",
            "boilerplate_lines_removed": 0,
            "redactions_inserted": 0,
            "cleaning_flags": [],
        }

    # Step 3 + 4 — per-template handling (metadata extract + structural cut)
    metadata: dict = {}
    if template == "FD-36":
        body, template_confidence = handle_fd36(raw)
    elif template == "FD-36-cont":
        body, template_confidence = handle_fd36_continuation(raw)
    elif template == "FD-350":
        body, metadata, template_confidence = handle_fd350(raw)
    elif template == "FD-xxx":
        body = handle_generic(raw)
    else:  # unknown — conservative: general rules only
        body = raw

    raw_line_count = len(raw.splitlines())
    after_template_cut = len(body.splitlines())

    # Step 5 — general pattern rules
    body = strip_control_chars(body)
    body, n_stamps = strip_searched_stamp(body)

    kept: list[str] = []
    n_dropped = 0
    for line in body.splitlines():
        if classify_drop(line) is None:
            kept.append(line)
        else:
            n_dropped += 1
    body = "\n".join(kept)

    body, n_redactions = label_redactions(body)

    # Step 6 — normalize LAST (so hyphen-rejoin doesn't glue boilerplate to body)
    clean_text = normalize_text(body)

    # Step 7 — fact-preservation guardrail
    clean_facts = extract_facts(clean_text)
    cleaning_flags: list[dict] = []
    for fact_type in ("dates", "dollars", "case_numbers"):
        lost = raw_facts[fact_type] - clean_facts[fact_type]
        if lost:
            cleaning_flags.append({"type": fact_type, "lost": sorted(lost)})

    boilerplate_removed = (
        (raw_line_count - after_template_cut) + n_dropped + n_stamps
    )

    return {
        **page,
        "template": template,
        "template_confidence": template_confidence,
        "extracted_metadata": metadata,
        "routed_to_missing_info": False,
        "clean_text": clean_text,
        "boilerplate_lines_removed": boilerplate_removed,
        "redactions_inserted": n_redactions,
        "cleaning_flags": cleaning_flags,
    }


# ============================================================================
# I/O + CLI
# ============================================================================


def load_pages(jsonl_path: Path) -> list[dict]:
    with jsonl_path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def write_pages(pages: list[dict], jsonl_path: Path) -> None:
    with jsonl_path.open("w", encoding="utf-8") as f:
        for p in pages:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")


def write_clean_txt(page: dict, out_dir: Path) -> None:
    if page.get("clean_text"):
        path = out_dir / f"page_{page['page_no']:03d}.txt"
        path.write_text(page["clean_text"], encoding="utf-8")


def print_template_distribution(pages: list[dict]) -> None:
    counts = Counter(p.get("template", "(not run)") for p in pages)
    routed = sum(1 for p in pages if p.get("routed_to_missing_info"))
    flagged = sum(1 for p in pages if p.get("cleaning_flags"))
    print("\nTemplate distribution:")
    for name, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        pages_of_type = sorted(p["page_no"] for p in pages if p.get("template") == name)
        sample = ", ".join(str(n) for n in pages_of_type[:8])
        if len(pages_of_type) > 8:
            sample += f", ... (+{len(pages_of_type) - 8} more)"
        print(f"  {name:<10} {count:>3}   pages: {sample}")
    print(f"\nRouted to missing-info:        {routed}")
    print(f"Tripped fact-preservation:    {flagged}")


def print_before_after(page: dict) -> None:
    """Smoke-test output for --pages mode."""
    print("=" * 72)
    print(f"PAGE {page['page_no']}   template={page.get('template')}   "
          f"confidence={page.get('template_confidence')}")
    if page.get("extracted_metadata"):
        print(f"extracted_metadata: {json.dumps(page['extracted_metadata'], ensure_ascii=False)}")
    print(f"boilerplate_lines_removed={page.get('boilerplate_lines_removed')}   "
          f"redactions_inserted={page.get('redactions_inserted')}   "
          f"routed_to_missing_info={page.get('routed_to_missing_info')}")
    if page.get("cleaning_flags"):
        print(f"!! cleaning_flags: {json.dumps(page['cleaning_flags'], ensure_ascii=False)}")
    print("-" * 36 + " RAW " + "-" * 31)
    print(page["raw_text"])
    print("-" * 35 + " CLEAN " + "-" * 30)
    print(page.get("clean_text") or "(routed out — no clean_text)")
    print()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("jsonl_path", type=Path)
    ap.add_argument("--detect-only", action="store_true",
                    help="Print template distribution only. Does not modify the JSONL.")
    ap.add_argument("--pages", type=str, default=None,
                    help="Comma-separated page numbers for smoke testing. "
                         "Prints before/after; does not modify the JSONL.")
    args = ap.parse_args()

    if not args.jsonl_path.exists():
        sys.exit(f"Not found: {args.jsonl_path}")

    pages = load_pages(args.jsonl_path)

    if not any("bucket" in p for p in pages):
        sys.exit("No 'bucket' field found. Run: python score_pages.py "
                 f"{args.jsonl_path} --threshold 60")

    # Detect-only: classify every clean-bucket page, print, exit.
    if args.detect_only:
        for p in pages:
            if p.get("bucket") == "clean":
                p["template"] = detect_template(p["raw_text"])
        print_template_distribution([p for p in pages if p.get("bucket") == "clean"])
        return

    # Smoke test: process named pages, print before/after, do NOT write back.
    if args.pages:
        wanted = {int(n.strip()) for n in args.pages.split(",")}
        for p in pages:
            if p["page_no"] in wanted and p.get("bucket") == "clean":
                cleaned = clean_page(p)
                print_before_after(cleaned)
        return

    # Full run: clean every clean-bucket page, write back, dump per-page txt files.
    out_dir = args.jsonl_path.parent / "clean"
    out_dir.mkdir(parents=True, exist_ok=True)

    updated: list[dict] = []
    for p in pages:
        if p.get("bucket") == "clean":
            cleaned = clean_page(p)
            updated.append(cleaned)
            write_clean_txt(cleaned, out_dir)
        else:
            updated.append(p)

    write_pages(updated, args.jsonl_path)

    clean_only = [p for p in updated if p.get("bucket") == "clean"]
    print_template_distribution(clean_only)
    print(f"\nWrote {sum(1 for p in clean_only if p.get('clean_text'))} cleaned txt files to {out_dir}")
    print(f"Updated {args.jsonl_path}")


if __name__ == "__main__":
    main()
