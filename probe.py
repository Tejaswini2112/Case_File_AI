"""
STEP 0 — Is your FBI PDF real text or a scanned image?

This is the first thing to run. It answers the one question that decides
your entire Phase 1 parsing approach. Costs nothing, takes 30 seconds.

Usage:
    1. Drop a PDF into data/raw/  (e.g. Bundy Part 1 from vault.fbi.gov)
    2. python probe.py data/raw/your-file.pdf

Read the verdict at the bottom of the output.
"""

import sys
from pathlib import Path

import fitz  # PyMuPDF

# Windows consoles default to cp1252 and choke on emoji in the verdict.
sys.stdout.reconfigure(encoding="utf-8")


def probe(pdf_path: str) -> None:
    path = Path(pdf_path)
    if not path.exists():
        print(f"❌ File not found: {path}")
        print("   Drop a PDF into data/raw/ and pass its path as an argument.")
        sys.exit(1)

    doc = fitz.open(path)
    print(f"📄 {path.name}")
    print(f"   Pages: {doc.page_count}")
    print("-" * 60)

    # Sample a few content pages (skip the cover, which is often blank/image).
    sample_pages = [p for p in (1, 5, 10, doc.page_count // 2) if p < doc.page_count]
    sample_pages = sorted(set(sample_pages))

    char_counts = []
    letter_ratios = []
    for p in sample_pages:
        text = doc[p].get_text()
        char_counts.append(len(text))
        letters = sum(c.isalpha() and c.isascii() for c in text)
        ratio = letters / len(text) if text else 0
        letter_ratios.append(ratio)
        preview = text.strip().replace("\n", " ")[:200]
        print(f"Page {p:>3}: {len(text):>5} chars | letters {ratio:>4.0%} | {preview!r}")

    print("-" * 60)
    avg = sum(char_counts) / len(char_counts) if char_counts else 0
    avg_ratio = sum(letter_ratios) / len(letter_ratios) if letter_ratios else 0
    print(f"Average chars/page across sample: {avg:.0f}")
    print(f"Average ASCII-letter ratio:       {avg_ratio:.0%}")
    print()

    # A high char count with a low letter ratio means the PDF has a text layer
    # but it's garbage (broken ToUnicode CMap, or a bad OCR layer baked into a
    # scan). Real prose runs ~70%+ letters; FBI Vault junk layers run 20–40%.
    looks_like_prose = avg_ratio >= 0.55

    if avg > 200 and looks_like_prose:
        print("✅ VERDICT: This is a TEXT PDF. You're on the easy path.")
        print("   → Proceed to Step 1: chunk + embed + query. No OCR needed.")
    elif avg > 200 and not looks_like_prose:
        print("🚨 VERDICT: TEXT LAYER IS GARBAGE. Likely a scan with a broken")
        print("   OCR layer baked in (common for FBI Vault PDFs).")
        print(f"   Only {avg_ratio:.0%} of extracted chars are letters — real prose is 70%+.")
        print("   → Treat as a scanned PDF. Re-OCR with Tesseract; ignore the")
        print("     embedded text layer entirely.")
    elif avg > 20:
        print("⚠️  VERDICT: MIXED / sparse text. Some pages may be scanned.")
        print("   → Spot-check more pages. You may need OCR for part of the doc.")
    else:
        print("🖼️  VERDICT: This is a SCANNED IMAGE PDF. No extractable text.")
        print("   → Phase 1 needs OCR. Uncomment pytesseract/pdf2image in")
        print("     requirements.txt and install Tesseract + poppler on Windows.")
        print("   → This is common for FBI Vault files — better to know now.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python probe.py <path-to-pdf>")
        print("Example: python probe.py data/raw/bundy-part-01.pdf")
        sys.exit(1)
    probe(sys.argv[1])
