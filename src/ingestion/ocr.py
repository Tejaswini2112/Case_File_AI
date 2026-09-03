"""
Step 1 — OCR every page of a scanned PDF.

Reads:  data/raw/<name>.pdf
Writes: data/ocr/<name>/page_NNN.txt    one text file per page (human-readable)
        data/ocr/<name>/page_NNN.json   per-page result + the inputs it came from
        data/ocr/<name>/pages.jsonl     one JSON row per page (machine-readable)

Each JSONL row carries the metrics needed to score and route the page later
(see score_pages.py) but does NOT yet assign a bucket — that decision comes
after looking at the histogram and picking a threshold.

Re-running is safe and skips finished pages. The per-page .json sidecars are
what make that possible: each records the result for one page plus a
fingerprint of the inputs that produced it, so a second run can tell which
pages are genuinely done. pages.jsonl is assembled from those sidecars at the
end rather than appended to as work proceeds.

Usage (run from project root):
    python src/ingestion/ocr.py data/raw/bundy-part-01.pdf
    python src/ingestion/ocr.py data/raw/bundy-part-01.pdf --force
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Iterator
from glob import glob
from pathlib import Path

import pytesseract
from pdf2image import convert_from_path, pdfinfo_from_path
from pdf2image.exceptions import PDFInfoNotInstalledError
from PIL import Image

# Windows console defaults to cp1252; OCR output contains non-ASCII.
sys.stdout.reconfigure(encoding="utf-8")

# Fallback paths for Windows installs that aren't on PATH (subprocesses often
# inherit a stale environment). On PATH these are silently ignored.
TESSERACT_FALLBACK = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
POPPLER_FALLBACK_GLOB = r"C:\Program Files\poppler\poppler-*\Library\bin"

# How many pages to render per poppler call. This is the memory dial: peak
# usage is roughly batch_size x one rendered page (~25 MB at 300 dpi), so 10
# pages costs ~250 MB regardless of how long the document is. Raising it buys
# slightly less poppler overhead at proportionally more memory.
RENDER_BATCH = 10

# 300 dpi is the standard sweet spot for typewritten text OCR.
# Higher = better quality but slower; lower = faster but error-prone on fine print.
DPI = 300

# Read size for hashing the source PDF. Hashing happens once per run, not per
# page, so this only needs to avoid loading a large PDF into memory whole.
HASH_CHUNK = 1 << 20

# FOIA exemption codes commonly stamped on FBI Vault releases:
# b1 (national security), b3 (statutory), b6 (personal privacy),
# b7C/b7D/b7E (law enforcement records). Match at word boundaries.
REDACTION_MARKER_RE = re.compile(r"\bb[1-9][A-E]?\b", re.IGNORECASE)


def ensure_tesseract() -> None:
    """If Tesseract isn't on PATH, fall back to the default Windows install."""
    try:
        pytesseract.get_tesseract_version()
    except pytesseract.TesseractNotFoundError:
        if Path(TESSERACT_FALLBACK).exists():
            pytesseract.pytesseract.tesseract_cmd = TESSERACT_FALLBACK
        else:
            sys.exit("Tesseract not found. Run tools_check.py for help.")


def find_poppler_bin() -> str | None:
    matches = sorted(glob(POPPLER_FALLBACK_GLOB), reverse=True)
    return matches[0] if matches else None


# ---------------------------------------------------------------------------
# Durable, repeatable page writes
# ---------------------------------------------------------------------------


def atomic_write_text(path: Path, text: str) -> None:
    """Write a file that either exists complete or does not exist at all.

    A plain write leaves a truncated file behind if the process dies partway,
    and a truncated file is indistinguishable from a finished one by any
    existence check — so a resumed run would skip it and carry on with silently
    corrupt text. Writing to a temporary name and renaming avoids that: the
    rename is atomic, so the real filename never names a partial file.

    os.replace rather than Path.rename because it overwrites an existing
    destination on Windows, where rename raises instead.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def source_fingerprint(pdf_path: Path) -> str:
    """Identify the inputs a page result was derived from.

    Cached pages must not be reused when they no longer describe the current
    inputs — a re-rendered PDF or a changed DPI produces different text, and
    silently serving the old result would be worse than redoing the work. The
    content hash rather than mtime or size because copying a file changes its
    timestamp without changing what OCR would produce, and two different PDFs
    can share a size.

    Hashed once per run and compared against every sidecar, so the cost is a
    single pass over the file against minutes of OCR.
    """
    digest = hashlib.sha256()
    with pdf_path.open("rb") as fh:
        while chunk := fh.read(HASH_CHUNK):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}:dpi{DPI}"


def sidecar_path(out_dir: Path, page_no: int) -> Path:
    return out_dir / f"page_{page_no:03d}.json"


def read_valid_sidecar(out_dir: Path, page_no: int, fingerprint: str) -> dict | None:
    """Return a page's stored row if it is present, parseable, current, and its
    text file still exists. Any doubt returns None, which redoes the page —
    redoing costs seconds, while trusting a bad record corrupts the corpus.
    """
    path = sidecar_path(out_dir, page_no)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if payload.get("fingerprint") != fingerprint:
        return None
    row = payload.get("row")
    if not isinstance(row, dict):
        return None
    # The row points at a text file that downstream stages read directly. A
    # sidecar without its text file is not a finished page.
    if not Path(row.get("text_path", "")).exists():
        return None
    return row


def write_sidecar(out_dir: Path, page_no: int, row: dict, fingerprint: str) -> None:
    payload = {"fingerprint": fingerprint, "row": row}
    atomic_write_text(
        sidecar_path(out_dir, page_no),
        json.dumps(payload, ensure_ascii=False, indent=None),
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def count_pages(pdf_path: Path, poppler_path: str | None) -> int:
    """Page count without rendering anything — pdfinfo reads the PDF's own
    structure. Needed up front now that the renderer no longer materialises a
    list we could take len() of."""
    try:
        return int(pdfinfo_from_path(str(pdf_path), poppler_path=poppler_path)["Pages"])
    except PDFInfoNotInstalledError:
        sys.exit("Poppler not found. Run tools_check.py for help.")


def iter_pages(
    pdf_path: Path,
    poppler_path: str | None,
    total: int,
    wanted: set[int],
    batch_size: int = RENDER_BATCH,
) -> Iterator[tuple[int, Image.Image]]:
    """Yield (page_no, image) for the wanted pages, rendering in small batches.

    This replaces a single convert_from_path() over the whole PDF. That call
    returned every page as a list, so peak memory was the entire document —
    measured at 2.7 GB for a 60-page file at 300 dpi, and linear in page count,
    which put a 1000-page release far beyond any laptop.

    Rendering in batches makes peak memory a function of batch_size instead of
    document length: a 10,000-page file costs the same as a 10-page one. Each
    batch is released before the next is rendered, because nothing holds a
    reference to it once its pages have been yielded and consumed.

    Batches rather than single pages because every convert_from_path call spawns
    poppler and re-reads the PDF's structure. Page-at-a-time would pay that cost
    once per page; ten at a time amortises it while still bounding memory.

    A batch containing no wanted page is never rendered. Rendering is around
    40% of the per-page cost, so a resumed run must skip it rather than render
    pages only to discard them.
    """
    for start in range(1, total + 1, batch_size):
        end = min(start + batch_size - 1, total)
        if not any(p in wanted for p in range(start, end + 1)):
            continue

        try:
            batch = convert_from_path(
                str(pdf_path),
                dpi=DPI,
                poppler_path=poppler_path,
                first_page=start,
                last_page=end,
            )
        except PDFInfoNotInstalledError:
            sys.exit("Poppler not found. Run tools_check.py for help.")

        for offset, image in enumerate(batch):
            page_no = start + offset
            if page_no in wanted:
                yield page_no, image

        # Drop the batch's last strong reference before rendering the next one.
        # Without this the previous batch stays alive across the loop boundary
        # while the next is being rendered, doubling the peak.
        del batch


# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------


def ocr_page(image: Image.Image) -> tuple[str, float]:
    """
    Return (text_with_line_breaks, mean_confidence_0_to_100).

    Uses a single image_to_data call so we get both text AND confidence in one
    OCR pass. Words are grouped by (block, paragraph, line) to reconstruct the
    layout — each Tesseract line becomes one line in the output.

    Tesseract returns -1 confidence for non-word blocks; those are filtered.
    """
    data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)

    lines: dict[tuple[int, int, int], list[str]] = {}
    confs: list[int] = []
    for word, conf, block, par, line in zip(
        data["text"], data["conf"], data["block_num"], data["par_num"], data["line_num"]
    ):
        conf = int(conf)
        if not word.strip() or conf < 0:
            continue
        lines.setdefault((block, par, line), []).append(word)
        confs.append(conf)

    text = "\n".join(" ".join(words) for words in lines.values())
    confidence = sum(confs) / len(confs) if confs else 0.0
    return text, confidence


def letter_ratio(text: str) -> float:
    if not text:
        return 0.0
    letters = sum(c.isalpha() and c.isascii() for c in text)
    return letters / len(text)


def find_redaction_markers(text: str) -> list[str]:
    """Unique FOIA exemption markers in the page text (e.g. ['b7c', 'b7d'])."""
    return sorted({m.lower() for m in REDACTION_MARKER_RE.findall(text)})


def build_row(page_no: int, pdf_path: Path, txt_path: Path, text: str, confidence: float) -> dict:
    """The pages.jsonl row for one page.

    Field order is load-bearing: pages.jsonl is compared byte-for-byte against
    previous runs to prove refactors do not change output, and json.dumps
    preserves insertion order.
    """
    return {
        "page_no": page_no,
        "source_file": str(pdf_path),
        "text_path": str(txt_path),
        "char_count": len(text),
        "letter_ratio": round(letter_ratio(text), 3),
        "ocr_confidence": round(confidence, 1),
        "redaction_markers_found": find_redaction_markers(text),
        "raw_text": text,
    }


def assemble_jsonl(out_dir: Path, jsonl_path: Path, total: int, fingerprint: str) -> int:
    """Write pages.jsonl from the per-page sidecars, in page order.

    Assembling at the end rather than appending as work proceeds is what lets
    pages be processed in any order, or by several workers at once, without a
    shared file to contend over or a half-written final line to recover from.
    """
    rows = []
    for page_no in range(1, total + 1):
        row = read_valid_sidecar(out_dir, page_no, fingerprint)
        if row is not None:
            rows.append(row)

    atomic_write_text(
        jsonl_path,
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
    )
    return len(rows)


def main(pdf_path: Path, force: bool = False) -> None:
    if not pdf_path.exists():
        sys.exit(f"Not found: {pdf_path}")

    ensure_tesseract()
    poppler_path = find_poppler_bin()

    out_dir = Path("data/ocr") / pdf_path.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = out_dir / "pages.jsonl"

    total = count_pages(pdf_path, poppler_path)
    fingerprint = source_fingerprint(pdf_path)

    if force:
        wanted = set(range(1, total + 1))
    else:
        wanted = {
            p for p in range(1, total + 1)
            if read_valid_sidecar(out_dir, p, fingerprint) is None
        }

    done = total - len(wanted)
    print(f"{pdf_path.name}: {total} pages at {DPI} dpi, {RENDER_BATCH} per render batch")
    if force:
        print(f"  --force: redoing all {total} pages")
    elif done:
        print(f"  resuming: {done} already done, {len(wanted)} to do")
    print("-" * 72)

    # Rendering is no longer a phase that finishes before OCR starts — pages are
    # rendered a batch at a time as OCR consumes them, so there is one timer for
    # the whole pass rather than a render figure and an OCR figure.
    t1 = time.time()
    for i, image in iter_pages(pdf_path, poppler_path, total, wanted):
        text, confidence = ocr_page(image)
        txt_path = out_dir / f"page_{i:03d}.txt"

        # Text first, then the sidecar that vouches for it. In this order a
        # crash between the two leaves an orphan text file, which the next run
        # simply overwrites. The reverse order would leave a sidecar claiming a
        # page is finished when its text file does not exist.
        atomic_write_text(txt_path, text)
        row = build_row(i, pdf_path, txt_path, text, confidence)
        write_sidecar(out_dir, i, row, fingerprint)

        print(
            f"  page {i:>3}/{total}  "
            f"chars={len(text):>5}  conf={confidence:>5.1f}  "
            f"letters={row['letter_ratio']:>4.0%}  "
            f"redactions={len(row['redaction_markers_found'])}"
        )

    written = assemble_jsonl(out_dir, jsonl_path, total, fingerprint)

    print("-" * 72)
    print(f"Render + OCR done in {time.time() - t1:.1f}s ({len(wanted)} pages processed).")
    print(f"Wrote {written} rows to {jsonl_path}")
    if written < total:
        print(f"  WARNING: {total - written} pages missing — re-run to finish them.")
    print(f"Next: python score_pages.py {jsonl_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="OCR every page of a scanned PDF.")
    ap.add_argument("pdf", type=Path, help="Path to the source PDF")
    ap.add_argument(
        "--force",
        action="store_true",
        help="Re-OCR every page, ignoring finished work from previous runs",
    )
    args = ap.parse_args()
    main(args.pdf, force=args.force)
