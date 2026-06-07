"""
Step 1 — OCR every page of a scanned PDF.

Reads:  data/raw/<name>.pdf
Writes: data/ocr/<name>/page_NNN.txt    one text file per page (human-readable)
        data/ocr/<name>/pages.jsonl     one JSON row per page (machine-readable)

Each JSONL row carries the metrics needed to score and route the page later
(see score_pages.py) but does NOT yet assign a bucket — that decision comes
after looking at the histogram and picking a threshold.

Usage (run from project root):
    python src/ocr.py data/raw/bundy-part-01.pdf
"""

import json
import re
import sys
import time
from glob import glob
from pathlib import Path

# Windows console defaults to cp1252; OCR output contains non-ASCII.
sys.stdout.reconfigure(encoding="utf-8")

import pytesseract
from pdf2image import convert_from_path
from pdf2image.exceptions import PDFInfoNotInstalledError
from PIL import Image

# Fallback paths for Windows installs that aren't on PATH (subprocesses often
# inherit a stale environment). On PATH these are silently ignored.
TESSERACT_FALLBACK = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
POPPLER_FALLBACK_GLOB = r"C:\Program Files\poppler\poppler-*\Library\bin"

# 300 dpi is the standard sweet spot for typewritten text OCR.
# Higher = better quality but slower; lower = faster but error-prone on fine print.
DPI = 300

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


def render_pages(pdf_path: Path, poppler_path: str | None) -> list[Image.Image]:
    try:
        return convert_from_path(str(pdf_path), dpi=DPI, poppler_path=poppler_path)
    except PDFInfoNotInstalledError:
        sys.exit("Poppler not found. Run tools_check.py for help.")


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


def main(pdf_path: Path) -> None:
    if not pdf_path.exists():
        sys.exit(f"Not found: {pdf_path}")

    ensure_tesseract()
    poppler_path = find_poppler_bin()

    out_dir = Path("data/ocr") / pdf_path.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = out_dir / "pages.jsonl"

    print(f"Rendering {pdf_path.name} at {DPI} dpi...")
    t0 = time.time()
    images = render_pages(pdf_path, poppler_path)
    print(f"Rendered {len(images)} pages in {time.time() - t0:.1f}s. Starting OCR...")
    print("-" * 72)

    t1 = time.time()
    with jsonl_path.open("w", encoding="utf-8") as jf:
        for i, image in enumerate(images, start=1):
            text, confidence = ocr_page(image)
            ratio = letter_ratio(text)
            markers = find_redaction_markers(text)

            txt_path = out_dir / f"page_{i:03d}.txt"
            txt_path.write_text(text, encoding="utf-8")

            row = {
                "page_no": i,
                "source_file": str(pdf_path),
                "text_path": str(txt_path),
                "char_count": len(text),
                "letter_ratio": round(ratio, 3),
                "ocr_confidence": round(confidence, 1),
                "redaction_markers_found": markers,
                "raw_text": text,
            }
            jf.write(json.dumps(row, ensure_ascii=False) + "\n")

            print(
                f"  page {i:>3}/{len(images)}  "
                f"chars={len(text):>5}  conf={confidence:>5.1f}  "
                f"letters={ratio:>4.0%}  redactions={len(markers)}"
            )

    print("-" * 72)
    print(f"OCR done in {time.time() - t1:.1f}s.")
    print(f"Wrote {len(images)} text files + {jsonl_path}")
    print(f"Next: python score_pages.py {jsonl_path}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("Usage: python ocr.py <path-to-pdf>")
    main(Path(sys.argv[1]))
