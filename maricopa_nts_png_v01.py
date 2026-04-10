"""
maricopa_nts_png_v01.py
Groups downloaded PNGs into one PDF per document number.
Input:  raw_png/{DOC_NUM}_p{PAGE}.png  (from scraper/download_pngs.py)
Output: grouped_output/pdfs/{DOC_NUM}.pdf
"""

import logging
import re
import sys
from collections import defaultdict
from pathlib import Path

log = logging.getLogger("nts_png")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

RAW_PNG_DIR    = Path("raw_png")
RENAMED_DIR    = Path("grouped_output/renamed")
PDF_OUTPUT_DIR = Path("grouped_output/pdfs")


def run():
    try:
        import img2pdf
        from PIL import Image
    except ImportError:
        log.error("Missing deps: pip install img2pdf pillow")
        sys.exit(1)

    RAW_PNG_DIR.mkdir(exist_ok=True)
    RENAMED_DIR.mkdir(parents=True, exist_ok=True)
    PDF_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Find all PNGs — support both naming conventions:
    #   {DOC_NUM}_p{PAGE}.png   (from download_pngs.py)
    #   image (N).png           (manually downloaded)
    pngs = sorted(RAW_PNG_DIR.glob("*.png")) + sorted(RAW_PNG_DIR.glob("*.PNG"))
    log.info(f"Found {len(pngs)} PNG files in {RAW_PNG_DIR}/")

    if not pngs:
        log.warning("No PNGs found — nothing to process")
        return

    # Group by document number
    grouped: dict[str, list[tuple[int, Path]]] = defaultdict(list)

    for png_path in pngs:
        name = png_path.stem

        # Pattern 1: {DOC_NUM}_p{PAGE}  e.g. 20260205851_p1
        m = re.match(r"^(\d{10,13})_p(\d+)$", name)
        if m:
            doc_num  = m.group(1)
            page_num = int(m.group(2))
            grouped[doc_num].append((page_num, png_path))
            continue

        # Pattern 2: manually named "image (N)" — OCR to find doc number
        m2 = re.match(r"^image[\s_\(]+(\d+)[\)\s]*$", name, re.I)
        if m2:
            doc_num = _ocr_doc_number(png_path)
            if not doc_num:
                doc_num = f"UNKNOWN_{m2.group(1):04d}"
            grouped[doc_num].append((int(m2.group(1)), png_path))
            continue

        # Fallback: use filename as doc number
        grouped[name].append((1, png_path))

    log.info(f"Grouped into {len(grouped)} document(s)")

    # Build one PDF per document
    success = 0
    for doc_num, pages in grouped.items():
        pdf_path = PDF_OUTPUT_DIR / f"{doc_num}.pdf"
        if pdf_path.exists():
            log.debug(f"  {doc_num}.pdf already exists — skipping")
            success += 1
            continue

        # Sort pages by page number
        pages.sort(key=lambda x: x[0])
        page_paths = [p for _, p in pages]

        try:
            page_bytes = [p.read_bytes() for p in page_paths]
            with open(pdf_path, "wb") as f:
                f.write(img2pdf.convert(page_bytes))
            log.info(f"  ✓ {pdf_path.name} ({len(pages)} page(s))")
            success += 1
        except Exception as exc:
            log.error(f"  ✗ PDF failed for {doc_num}: {exc}")
            # Fallback: save as multi-page TIFF
            try:
                imgs = [Image.open(p).convert("RGB") for p in page_paths]
                tif_path = PDF_OUTPUT_DIR / f"{doc_num}.tif"
                imgs[0].save(tif_path, save_all=True, append_images=imgs[1:])
                log.info(f"  ✓ Saved as TIFF fallback: {tif_path.name}")
                success += 1
            except Exception as exc2:
                log.error(f"  ✗ TIFF fallback also failed: {exc2}")

    log.info(f"Complete: {success}/{len(grouped)} PDFs created in {PDF_OUTPUT_DIR}/")


def _ocr_doc_number(png_path: Path) -> str | None:
    """OCR a PNG to extract the Maricopa document recording number."""
    try:
        import pytesseract
        from PIL import Image
        img  = Image.open(png_path).convert("L")
        text = pytesseract.image_to_string(img, config="--psm 6")
        m = re.search(r"\b(20\d{2}\d{7,9})\b", text)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


if __name__ == "__main__":
    run()
