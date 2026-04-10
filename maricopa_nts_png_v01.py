"""
maricopa_nts_png_v01.py
Processes raw PNG images from Maricopa County NTS (Notice of Trustee Sale).
Groups multi-page notices by document number, outputs individual PDFs.

Pipeline:
  raw_png/ → OCR → grouped_output/renamed/ → grouped_output/pdfs/

Dependencies:
  pip install pillow pytesseract img2pdf
  apt-get install tesseract-ocr (or brew install tesseract)
"""

import logging
import re
import sys
from pathlib import Path

log = logging.getLogger("nts_png")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ── config ─────────────────────────────────────────────────────────────────────
RAW_PNG_DIR     = Path("raw_png")
RENAMED_DIR     = Path("grouped_output/renamed")
PDF_OUTPUT_DIR  = Path("grouped_output/pdfs")
DPI             = 300
UNKNOWN_PREFIX  = "UNKNOWN"


def run():
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        log.error("Missing deps: pip install pillow pytesseract img2pdf")
        sys.exit(1)

    RAW_PNG_DIR.mkdir(exist_ok=True)
    RENAMED_DIR.mkdir(parents=True, exist_ok=True)
    PDF_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    pngs = sorted(RAW_PNG_DIR.glob("*.png")) + sorted(RAW_PNG_DIR.glob("*.PNG"))
    log.info(f"Found {len(pngs)} PNG files in {RAW_PNG_DIR}")

    if not pngs:
        log.warning("No PNGs found — place images in raw_png/ and re-run")
        return

    # ── OCR each image → extract doc number ───────────────────────────────
    grouped: dict[str, list[Path]] = {}

    for i, png_path in enumerate(pngs):
        log.info(f"[{i+1}/{len(pngs)}] OCR: {png_path.name}")
        try:
            img  = _load_and_clean(png_path)
            text = pytesseract.image_to_string(img, config="--psm 6")
            doc_num = _extract_doc_number(text)
        except Exception as exc:
            log.warning(f"  OCR failed: {exc}")
            doc_num = None

        label = doc_num if doc_num else f"{UNKNOWN_PREFIX}_{i+1:04d}"

        # Rename/copy to renamed dir
        renamed_path = RENAMED_DIR / f"{label}_p{i+1:03d}.png"
        try:
            img_pil = _load_and_clean(png_path)
            img_pil.save(renamed_path, dpi=(DPI, DPI))
        except Exception as exc:
            log.warning(f"  Save renamed failed: {exc} — using raw copy")
            import shutil
            shutil.copy2(png_path, renamed_path)

        grouped.setdefault(label, []).append(renamed_path)

    log.info(f"Grouped into {len(grouped)} document(s)")

    # ── combine pages per doc number → PDF ────────────────────────────────
    try:
        import img2pdf
    except ImportError:
        log.error("img2pdf not installed — pip install img2pdf")
        _fallback_pdf(grouped)
        return

    for doc_num, pages in grouped.items():
        pdf_path = PDF_OUTPUT_DIR / f"{doc_num}.pdf"
        try:
            page_bytes = [open(p, "rb").read() for p in sorted(pages)]
            with open(pdf_path, "wb") as f:
                f.write(img2pdf.convert(page_bytes))
            log.info(f"  → {pdf_path} ({len(pages)} page(s))")
        except Exception as exc:
            log.error(f"  PDF creation failed for {doc_num}: {exc}")

    log.info("PNG processing complete.")
    log.info(f"PDFs → {PDF_OUTPUT_DIR}")


def _load_and_clean(png_path: Path):
    """Load PNG, convert to grayscale, apply basic cleanup."""
    from PIL import Image, ImageFilter, ImageEnhance
    img = Image.open(png_path).convert("L")

    # Sharpen + contrast
    img = ImageEnhance.Contrast(img).enhance(2.0)
    img = img.filter(ImageFilter.SHARPEN)
    return img


def _extract_doc_number(text: str) -> str | None:
    """
    Maricopa doc numbers look like: 2026XXXXXXXXX (13 digits starting with year)
    or older 9-digit format.
    """
    patterns = [
        r"\b(20\d{2}\d{7,9})\b",          # 2024XXXXXXX
        r"[Dd]oc(?:ument)?\s*#?\s*:?\s*([\d]{7,13})",
        r"[Rr]ecording\s+[Nn]o\.?\s*:?\s*([\d]{7,13})",
        r"[Ii]nstrument\s+[Nn]o\.?\s*:?\s*([\d]{7,13})",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return m.group(1).strip()
    return None


def _fallback_pdf(grouped: dict):
    """Use Pillow to save images as multi-page TIFF if img2pdf unavailable."""
    from PIL import Image
    for doc_num, pages in grouped.items():
        pdf_path = PDF_OUTPUT_DIR / f"{doc_num}.tif"
        try:
            imgs = [Image.open(p).convert("RGB") for p in sorted(pages)]
            if imgs:
                imgs[0].save(pdf_path, save_all=True, append_images=imgs[1:])
                log.info(f"  → {pdf_path} (TIFF fallback, {len(imgs)} pages)")
        except Exception as exc:
            log.error(f"  TIFF fallback failed for {doc_num}: {exc}")


if __name__ == "__main__":
    run()
