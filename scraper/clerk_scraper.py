"""
scraper/clerk_scraper.py  v8 — CONFIRMED WORKING
API: https://publicapi.recorder.maricopa.gov/documents/search
Confirmed constraints:
  - pageSize max = 200
  - maxResults max = 200
  - Requires Referer + Origin headers from recorder.maricopa.gov
  - Date format: YYYY-MM-DD
  - pageSize=200 returns all results in one call for typical weekly pulls
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
PORTAL_BASE = "https://recorder.maricopa.gov"

PAGE_SIZE   = 200   # confirmed API max
MAX_RESULTS = 200   # confirmed API max — paginate to get more if needed

MAX_RETRIES   = 3
RETRY_DELAY   = 5
REQUEST_DELAY = 0.5

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
    "User-Agent":      "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         f"{PORTAL_BASE}/recording/document-search-results.html",
    "Origin":          PORTAL_BASE,
    "Connection":      "keep-alive",
})


class ClerkScraper:
    def __init__(self, lead_types: dict, start_date: str, end_date: str):
        self.lead_types = lead_types
        self.start_date = start_date
        self.end_date   = end_date
        self.records: list[dict] = []

    async def run(self) -> list[dict]:
        # Warm up session cookies via portal
        try:
            SESSION.get(f"{PORTAL_BASE}/recording/document-search.html", timeout=15)
            log.info("Session warmed up via portal")
        except Exception as e:
            log.warning(f"Portal warmup failed (non-fatal): {e}")

        for lead_key in self.lead_types:
            doc_code       = DOC_CODES.get(lead_key)
            cat, cat_label = self.lead_types[lead_key]
            log.info(f"Scraping {lead_key} ({cat_label})")
            if not doc_code:
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

    def _fetch_all(self, lead_key, doc_code, cat, cat_label) -> list[dict]:
        all_records = []
        page = 1

        while True:
            params = {
                "documentCode": doc_code,
                "beginDate":    self.start_date,
                "endDate":      self.end_date,
                "pageSize":     PAGE_SIZE,
                "pageNumber":   page,
                "maxResults":   MAX_RESULTS,
            }
            data = self._get(SEARCH_URL, params)
            if data is None:
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
                    log.debug(f"  Row error: {exc}")

            # If total > 200, paginate
            if len(all_records) >= total or len(results) < PAGE_SIZE:
                break
            page += 1
            time.sleep(REQUEST_DELAY)

        return all_records

    def _item_to_record(self, item: dict, lead_key, cat, cat_label) -> Optional[dict]:
        doc_num = str(item.get("recordingNumber", "")).strip()
        if not doc_num:
            return None

        return {
            "doc_num":       doc_num,
            "doc_type":      item.get("documentCode", cat),
            "filed":         _norm_date(item.get("recordingDate", "")),
            "cat":           cat,
            "cat_label":     cat_label,
            "lead_key":      lead_key,
            "owner":         item.get("names", "") or "",
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
            "pdf_url":       f"{PORTAL_BASE}/recording/document-preview.html?recNum={doc_num}",
            "clerk_url":     f"{PORTAL_BASE}/recording/document-details?id={doc_num}",
            "flags": [], "score": 0,
        }

    def _get(self, url: str, params: dict) -> Optional[dict]:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = SESSION.get(url, params=params, timeout=20)
                if resp.ok:
                    return resp.json()
                log.warning(f"  HTTP {resp.status_code} — {resp.text[:200]}")
            except Exception as exc:
                log.warning(f"  Attempt {attempt} failed: {exc}")
            time.sleep(RETRY_DELAY * attempt)
        return None


def _norm_date(raw: str) -> str:
    raw = raw.strip().replace("-", "/")
    for fmt in ("%m/%d/%Y", "%Y/%m/%d", "%m/%d/%y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw
