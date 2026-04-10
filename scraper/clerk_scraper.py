"""
scraper/clerk_scraper.py  v6 — FINAL (API-based)

Uses the confirmed public Maricopa Recorder API directly.
No browser automation needed for search — pure requests.

Confirmed endpoints (from live network inspection):
  Search: GET https://publicapi.recorder.maricopa.gov/documents/search
    Params: documentCode, beginDate (YYYY-MM-DD), endDate (YYYY-MM-DD),
            pageSize, pageNumber, maxResults
  Detail: GET https://publicapi.recorder.maricopa.gov/documents/{recordingNumber}
    Returns: names[], documentCodes[], recordingDate, recordingNumber,
             pageAmount, restricted

Confirmed doc code values (from live Select2 options):
  NS = Notice of Trustees Sale
  FL = Federal Tax Lien
  SL = State Tax Lien
  DE = Tax Deed
  PD = Probate Deed
  PJ = Probate (general)
"""

import asyncio
import logging
import re
import time
from datetime import datetime
from typing import Optional

import requests

log = logging.getLogger("clerk_scraper")

API_BASE    = "https://publicapi.recorder.maricopa.gov"
SEARCH_URL  = f"{API_BASE}/documents/search"
DETAIL_URL  = f"{API_BASE}/documents"
PORTAL_BASE = "https://recorder.maricopa.gov"

PAGE_SIZE   = 500   # max per request — reduces round trips
MAX_RESULTS = 9999

MAX_RETRIES   = 3
RETRY_DELAY   = 3
REQUEST_DELAY = 0.3   # polite delay between requests

DOC_CODES = {
    "NS": "NS",
    "FL": "FL",
    "SL": "SL",
    "DE": "DE",
    "PD": "PD",
    "PJ": "PJ",
}

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept":  "application/json, */*",
    "Referer": "https://recorder.maricopa.gov/recording/document-search-results.html",
    "Origin":  "https://recorder.maricopa.gov",
})


class ClerkScraper:
    def __init__(self, lead_types: dict, start_date: str, end_date: str):
        self.lead_types = lead_types
        self.start_date = start_date   # YYYY-MM-DD
        self.end_date   = end_date
        self.records: list[dict] = []

    # ── entry point (kept async for compatibility with fetch.py) ──────────
    async def run(self) -> list[dict]:
        for lead_key in self.lead_types:
            doc_code       = DOC_CODES.get(lead_key)
            cat, cat_label = self.lead_types[lead_key]
            log.info(f"Scraping {lead_key} ({cat_label})")

            if not doc_code:
                log.warning(f"No doc code for {lead_key} — skipping")
                continue

            try:
                recs = self._fetch_all(lead_key, doc_code, cat, cat_label)
                log.info(f"  → {len(recs)} records for {lead_key}")
                self.records.extend(recs)
            except Exception as exc:
                log.error(f"  ✗ Failed {lead_key}: {exc}", exc_info=True)

            time.sleep(REQUEST_DELAY)

        log.info(f"Total records: {len(self.records)}")
        return self.records

    # ── fetch all pages for one doc type ──────────────────────────────────
    def _fetch_all(self, lead_key, doc_code, cat, cat_label) -> list[dict]:
        params = {
            "documentCode": doc_code,
            "beginDate":    self.start_date,
            "endDate":      self.end_date,
            "pageSize":     PAGE_SIZE,
            "pageNumber":   1,
            "maxResults":   MAX_RESULTS,
        }

        all_records = []
        page = 1

        while True:
            params["pageNumber"] = page
            data = self._get(SEARCH_URL, params)
            if data is None:
                log.warning(f"  API returned None for {lead_key} page {page}")
                break

            results = data.get("searchResults", [])
            total   = data.get("totalResults", 0)
            log.info(f"  Page {page}: {len(results)} rows (total={total})")

            for item in results:
                try:
                    rec = self._item_to_record(item, lead_key, cat, cat_label)
                    if rec:
                        all_records.append(rec)
                except Exception as exc:
                    log.debug(f"  Row parse error: {exc}")

            # Paginate
            if len(all_records) >= total or len(results) < PAGE_SIZE:
                break
            page += 1
            time.sleep(REQUEST_DELAY)

        return all_records

    # ── convert one API result item → record dict ──────────────────────────
    def _item_to_record(self, item: dict, lead_key, cat, cat_label) -> Optional[dict]:
        doc_num = str(item.get("recordingNumber", "")).strip()
        if not doc_num:
            return None

        filed    = _norm_date(item.get("recordingDate", ""))
        doc_type = item.get("documentCode", cat)
        # names field from search is often empty; enricher fills from detail API
        names_raw = item.get("names", "") or ""

        clerk_url = (
            f"{PORTAL_BASE}/recording/document-details?"
            f"id={doc_num}"
        )
        pdf_url = (
            f"{PORTAL_BASE}/recording/document-preview.html?"
            f"recNum={doc_num}"
        )

        return {
            "doc_num":       doc_num,
            "doc_type":      doc_type,
            "filed":         filed,
            "cat":           cat,
            "cat_label":     cat_label,
            "lead_key":      lead_key,
            "owner":         names_raw if isinstance(names_raw, str) else "",
            "grantee":       "",
            "amount":        None,
            "legal":         "",
            "prop_address":  None, "prop_city":    None,
            "prop_state":    "AZ", "prop_zip":     None,
            "mail_address":  None, "mail_city":    None,
            "mail_state":    None, "mail_zip":     None,
            "parcel":        None,
            "first_name":    None, "last_name":    None,
            "first_name_2":  None, "last_name_2":  None,
            "trustee_name":  None, "trustee_phone": None,
            "auction_date":  None,
            "pdf_url":       pdf_url,
            "clerk_url":     clerk_url,
            "flags": [], "score": 0,
        }

    # ── HTTP GET with retry ────────────────────────────────────────────────
    def _get(self, url: str, params: dict = None) -> Optional[dict]:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = SESSION.get(url, params=params, timeout=20)
                if resp.ok:
                    return resp.json()
                log.warning(f"  HTTP {resp.status_code} for {url}")
            except Exception as exc:
                log.warning(f"  Request attempt {attempt} failed: {exc}")
            time.sleep(RETRY_DELAY * attempt)
        return None


# ── utilities ──────────────────────────────────────────────────────────────────
def _norm_date(raw: str) -> str:
    """Handle M-DD-YYYY, MM/DD/YYYY, YYYY-MM-DD."""
    raw = raw.strip().replace("-", "/")
    for fmt in ("%m/%d/%Y", "%Y/%m/%d", "%m/%d/%y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw
