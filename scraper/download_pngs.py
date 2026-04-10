"""
scraper/download_pngs.py
Downloads page 1 + page 2 PNGs for every NTS record in data/records.json
and saves them to raw_png/ for grouping into PDFs.

PNG API (confirmed working, no login required):
  GET https://publicapi.recorder.maricopa.gov/preview/image
      ?recordingNumber={DOC_NUM}&suffix=&affidavit=false&pageNumber=1
"""

import json
import logging
import sys
import time
from pathlib import Path

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("download_pngs")

PNG_API    = "https://publicapi.recorder.maricopa.gov/preview/image"
PORTAL_BASE = "https://recorder.maricopa.gov"
RAW_PNG_DIR = Path("raw_png")
RECORDS_PATH = Path("data/records.json")

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Referer":    f"{PORTAL_BASE}/recording/document-preview.html",
    "Origin":     PORTAL_BASE,
    "Accept":     "image/png,*/*",
})


def download_png(doc_num: str, page_num: int) -> bytes | None:
    params = {
        "recordingNumber": doc_num,
        "suffix":          "",
        "affidavit":       "false",
        "pageNumber":      page_num,
    }
    for attempt in range(1, 4):
        try:
            resp = SESSION.get(PNG_API, params=params, timeout=20)
            if resp.ok and "image" in resp.headers.get("content-type", ""):
                return resp.content
            log.debug(f"  {doc_num} p{page_num}: HTTP {resp.status_code}")
            return None
        except Exception as exc:
            log.debug(f"  Download attempt {attempt}: {exc}")
            time.sleep(2 * attempt)
    return None


def main():
    RAW_PNG_DIR.mkdir(exist_ok=True)

    if not RECORDS_PATH.exists():
        log.warning(f"{RECORDS_PATH} not found — skipping PNG download")
        return

    data = json.loads(RECORDS_PATH.read_text())
    records = data.get("records", [])

    # Only download PNGs for NTS records (they have the most useful document text)
    nts_records = [r for r in records if r.get("lead_key") == "NS"]
    log.info(f"Downloading PNGs for {len(nts_records)} NTS records...")

    downloaded = 0
    for i, rec in enumerate(nts_records):
        doc_num    = rec.get("doc_num", "")
        page_count = 2  # download first 2 pages of each NTS doc

        for page_num in range(1, page_count + 1):
            out_path = RAW_PNG_DIR / f"{doc_num}_p{page_num}.png"

            # Skip if already downloaded
            if out_path.exists():
                log.debug(f"  [{i+1}/{len(nts_records)}] {doc_num} p{page_num} already exists")
                continue

            png = download_png(doc_num, page_num)
            if png:
                out_path.write_bytes(png)
                downloaded += 1
                log.info(f"  [{i+1}/{len(nts_records)}] ✓ {doc_num} p{page_num} ({len(png)//1024}KB)")
            else:
                log.debug(f"  [{i+1}/{len(nts_records)}] ✗ {doc_num} p{page_num} — not available")

            time.sleep(0.3)  # polite delay

    log.info(f"PNG download complete: {downloaded} files saved to {RAW_PNG_DIR}/")


if __name__ == "__main__":
    main()
