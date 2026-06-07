"""
Step 3 — Template-aware OCR cleaner.

Reads:  data/ocr/<name>/pages.jsonl   (must have bucket field — run score_pages.py first)
Writes: data/ocr/<name>/pages.jsonl   (adds clean_text + diagnostic fields)
        data/ocr/<name>/clean/page_NNN.txt   (per page, for human diffing)

Only processes pages where bucket == "clean". 4-750 DELETED-PAGE sheets, even
when they land in the clean bucket, are routed to missing-info instead of cleaned.

See CaseFile-AI-Cleaner-Spec.md for the design.

Usage:
    python clean_pages.py data/ocr/bundy-part-01/pages.jsonl
    python clean_pages.py data/ocr/bundy-part-01/pages.jsonl --detect-only
    python clean_pages.py data/ocr/bundy-part-01/pages.jsonl --pages 4,15,47
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
TEMPLATE_PATTERNS = [
    (re.compile(r"\b4[-.]?\s*750\b"), "4-750"),
    (re.compile(r"\bFD[-.]?\s*36\b(?!\d)"), "FD-36"),
    (re.compile(r"\bFD[-.]?\s*350\b"), "FD-350"),
    (PAGE_CONTINUATION_RE, "FD-36-cont"),
    (re.compile(r"\bFD[-.]?\s*\d{1,3}\b"), "FD-xxx"),
]


def detect_template(text: str) -> str:
    """Look at the first 12 lines for an FBI form number. Returns template name or 'unknown'."""
    head = "\n".join(text.splitlines()[:12])
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
    """Pull dates, dollar amounts, and case file numbers."""
    dates: set[str] = set()
    for p in DATE_PATTERNS:
        dates.update(m.group(0) for m in p.finditer(text))
    return {
        "dates": dates,
        "dollars": {m.group(0) for m in DOLLAR_PATTERN.finditer(text)},
        "case_numbers": {m.group(0) for m in CASE_NUMBER_PATTERN.finditer(text)},
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
    """True if the line carries a date, dollar amount, or case file number."""
    if DOLLAR_PATTERN.search(line) or CASE_NUMBER_PATTERN.search(line):
        return True
    return any(p.search(line) for p in DATE_PATTERNS)


def classify_drop(line: str) -> str | None:
    """
    Decide whether to drop a line. Returns:
        'orphan' — short stray fragment (1-3 chars with symbols, or meaningless)
        'soup'   — long-enough line whose letter ratio is below 40% (OCR noise)
        None     — keep
    """
    stripped = line.strip()
    if not stripped:
        return None  # blank lines handled by normalize

    # Change #1: PROTECT lines carrying structured case facts. A line like
    # "11/24/78" alone has 0% letters but is real data — never drop it.
    if line_contains_fact(stripped):
        return None

    if len(stripped) <= 3:
        if stripped in SHORT_KEEPLIST:
            return None
        # Anything else this short is almost certainly OCR noise on its own line.
        return "orphan"

    if ascii_letter_ratio(stripped) < 0.40:
        return "soup"

    return None


# Stamps and form furniture that can appear on many template types.
SEARCHED_STAMP_RE = re.compile(r"\b(?:SEARCHED|SERIALIZED|INDEXED|FILED)\b", re.IGNORECASE)
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
        if SEARCHED_STAMP_RE.search(line):
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
    # The remaining keywords each catch every checkbox line on page 4
    # because each line contains at least one of them.
    re.compile(
        r"^.*\b(?:Teletype|Facsimile|Airtel|TOP\s+SECRET|CONFIDENTIAL|EFTO)\b.*$",
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


def handle_fd36(raw: str) -> tuple[str, str]:
    """
    Preferred: structural cut between the first and last BT markers.
    Fallback (when BT markers are missing or garbled): pattern-strip the
    named FD-36 furniture blocks. The safety net catches over-stripping.

    Returns (body, confidence). 'high' = clean structural cut; 'low' = pattern fallback.
    """
    lines = raw.splitlines()
    bt_indices = [i for i, line in enumerate(lines) if re.search(r"\bBT\b", line)]
    if len(bt_indices) >= 2:
        body = lines[bt_indices[0] + 1 : bt_indices[-1]]
        return "\n".join(body), "high"
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
    n_soup = n_orphan = 0
    for line in body.splitlines():
        reason = classify_drop(line)
        if reason == "soup":
            n_soup += 1
        elif reason == "orphan":
            n_orphan += 1
        else:
            kept.append(line)
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
        (raw_line_count - after_template_cut) + n_soup + n_orphan + n_stamps
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
