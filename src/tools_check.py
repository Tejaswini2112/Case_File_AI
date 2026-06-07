"""
Verify the OCR toolchain is wired up before building ocr.py.

Checks, in order:
    1. pytesseract + pdf2image installed in the active venv
    2. Tesseract binary is reachable (PATH, or via fallback path)
    3. Poppler is reachable (pdf2image can render a page)
    4. End-to-end OCR works on page 1 of the Bundy PDF

Usage (run from project root):
    python src/tools_check.py

If anything fails, the error message tells you exactly what to fix.
"""

import sys
from pathlib import Path

# Windows console defaults to cp1252 and chokes on non-ASCII in OCR output.
sys.stdout.reconfigure(encoding="utf-8")

PDF_PATH = Path("data/raw/bundy-part-01.pdf")

# Common Windows install locations. Used as fallbacks if the binaries aren't on
# PATH but are installed in the default spots.
TESSERACT_FALLBACK = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
POPPLER_FALLBACK_GLOB = r"C:\Program Files\poppler\poppler-*\Library\bin"


def find_poppler_bin() -> str | None:
    """Return the first poppler bin directory matching the fallback glob, or None."""
    from glob import glob
    matches = sorted(glob(POPPLER_FALLBACK_GLOB), reverse=True)  # newest version first
    return matches[0] if matches else None


def check(label: str, ok: bool, detail: str = "") -> None:
    mark = "[ OK ]" if ok else "[FAIL]"
    print(f"{mark} {label}" + (f"  -- {detail}" if detail else ""))
    if not ok:
        sys.exit(1)


print("=" * 60)
print("OCR toolchain check")
print("=" * 60)

# --- 1. Python packages ---
try:
    import pytesseract
    from pdf2image import convert_from_path
    from pdf2image.exceptions import PDFInfoNotInstalledError
except ImportError as e:
    print(f"[FAIL] Python package missing: {e}")
    print("       Run: pip install -r requirements.txt")
    sys.exit(1)
check("pytesseract + pdf2image importable", True)

# --- 2. Tesseract binary ---
try:
    version = pytesseract.get_tesseract_version()
except pytesseract.TesseractNotFoundError:
    # Fall back to the default Windows install path before giving up.
    if Path(TESSERACT_FALLBACK).exists():
        pytesseract.pytesseract.tesseract_cmd = TESSERACT_FALLBACK
        try:
            version = pytesseract.get_tesseract_version()
            print(f"[INFO] Tesseract not on PATH; using fallback: {TESSERACT_FALLBACK}")
        except Exception as e:
            check("Tesseract reachable", False, str(e))
    else:
        print("[FAIL] Tesseract not found on PATH and not at default install location.")
        print("       Install from: https://github.com/UB-Mannheim/tesseract/wiki")
        print(f"       Or set: pytesseract.pytesseract.tesseract_cmd = r\"{TESSERACT_FALLBACK}\"")
        sys.exit(1)
check("Tesseract reachable", True, f"v{version}")

# --- 3. Input file exists ---
check("Bundy PDF exists", PDF_PATH.exists(), str(PDF_PATH))

# --- 4. Poppler / pdf2image: render page 1 ---
poppler_path = None
try:
    images = convert_from_path(str(PDF_PATH), first_page=1, last_page=1, dpi=150)
except PDFInfoNotInstalledError:
    # Not on PATH — try the default install location before giving up.
    poppler_path = find_poppler_bin()
    if poppler_path:
        try:
            images = convert_from_path(
                str(PDF_PATH), first_page=1, last_page=1, dpi=150, poppler_path=poppler_path
            )
            print(f"[INFO] Poppler not on PATH; using fallback: {poppler_path}")
        except Exception as e:
            check("Poppler renders page 1", False, str(e))
    else:
        print("[FAIL] Poppler not found on PATH and not at default install location.")
        print("       Install from: https://github.com/oschwartz10612/poppler-windows/releases")
        print("       Then add the 'Library\\bin' subfolder of the extracted archive to your User PATH,")
        print(r"       or place it under C:\Program Files\poppler\poppler-*\Library\bin")
        sys.exit(1)
except Exception as e:
    check("Poppler renders page 1", False, str(e))

check("Poppler renders page 1", True, f"image size = {images[0].size}")

# --- 5. End-to-end OCR ---
text = pytesseract.image_to_string(images[0])
sample = text.strip().replace("\n", " ")[:200]
check("Tesseract OCRs page 1", bool(text.strip()), f"chars = {len(text)}")
print()
print("Page 1 OCR sample (first 200 chars):")
print(f"  {sample!r}")
print()
print("=" * 60)
print("All checks passed. Ready to build ocr.py.")
print("=" * 60)
