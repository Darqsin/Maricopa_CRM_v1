"""
scraper/clerk_scraper.py
Playwright-based async scraper for Maricopa County Recorder document search.
URL: https://recorder.maricopa.gov/recording/document-search.html
"""

import asyncio
import logging
import re
import time
from datetime import datetime
from typing import Optional

log = logging.getLogger("clerk_scraper")

# Doc-type → recorder search category mapping
# These are the actual form values used on the Maricopa recorder portal
DOC_TYPE_MAP = {
    "NS": {"category": "NOTS", "search_type": "NOTS"},   # Notice of Trustee Sale
    "FL": {"category": "LIEN", "search_type": "FTLF"},   # Federal Tax Lien Filed
    "SL": {"category": "LIEN", "search_type": "SLIEN"},  # State Tax Lien
    "DE": {"category": "TAX",  "search_type": "TDEED"},  # Tax Deed
    "PD": {"category": "PRO",  "search_type": "PROBJ"},  # Probate (deceased)
    "PJ": {"category": "PRO",  "search_type": "PROJD"},  # Probate judgment
}

BASE_URL    = "https://recorder.maricopa.gov"
SEARCH_URL  = f"{BASE_URL}/recording/document-search.html"
MAX_RETRIES = 3
PAGE_TIMEOUT = 30_000   # ms


class ClerkScraper:
    def __init__(self, lead_types: dict, start_date: str, end_date: str):
        self.lead_types  = lead_types        # {"NS": ("NOTS", "Notice…"), …}
        self.start_date  = start_date        # "YYYY-MM-DD"
        self.end_date    = end_date
        self.records: list[dict] = []

    # ── public entry point ─────────────────────────────────────────────────
    async def run(self) -> list[dict]:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox"],
            )
            ctx = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                )
            )
            page = await ctx.new_page()
            page.set_default_timeout(PAGE_TIMEOUT)

            for lead_key in self.lead_types:
                if lead_key not in DOC_TYPE_MAP:
                    log.warning(f"No DOC_TYPE_MAP entry for lead key '{lead_key}' — skipping")
                    continue
                cfg = DOC_TYPE_MAP[lead_key]
                cat_label = self.lead_types[lead_key][1]
                log.info(f"Scraping {lead_key} ({cat_label}) …")
                try:
                    recs = await self._scrape_type(page, lead_key, cfg)
                    log.info(f"  → {len(recs)} records for {lead_key}")
                    self.records.extend(recs)
                except Exception as exc:
                    log.error(f"  ✗ Failed scraping {lead_key}: {exc}", exc_info=True)

            await browser.close()

        log.info(f"Total clerk records: {len(self.records)}")
        return self.records

    # ── scrape one document type ───────────────────────────────────────────
    async def _scrape_type(self, page, lead_key: str, cfg: dict) -> list[dict]:
        cat, search_type = cfg["category"], cfg["search_type"]
        cat_label  = self.lead_types[lead_key][1]

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                await page.goto(SEARCH_URL, wait_until="domcontentloaded")
                await asyncio.sleep(1)

                # ── fill the search form ───────────────────────────────────
                # Select document type category tab / dropdown
                # The recorder uses a tabbed interface; we target the
                # "Document Type" search tab.
                await self._select_search_tab(page, "Document Type")

                # Fill date range
                await self._fill_date_range(page, self.start_date, self.end_date)

                # Select category (NOTS / LIEN / TAX / PRO)
                await self._select_category(page, cat)

                # Select specific doc sub-type if dropdown exists
                await self._select_doc_subtype(page, search_type)

                # Submit
                await self._submit_search(page)

                # Wait for results
                await page.wait_for_selector(
                    "table.resultsTable, #searchResults, .no-results, #tblResults",
                    timeout=20_000,
                )
                await asyncio.sleep(0.5)

                # Check for no-results
                no_res = page.locator(".no-results, .noResults")
                if await no_res.count() > 0:
                    log.info(f"  No results for {lead_key}")
                    return []

                # Parse result pages
                records = await self._parse_all_pages(page, lead_key, cat, cat_label)
                return records

            except Exception as exc:
                log.warning(f"  Attempt {attempt}/{MAX_RETRIES} failed for {lead_key}: {exc}")
                if attempt == MAX_RETRIES:
                    log.error(f"  All retries exhausted for {lead_key}")
                    return []
                await asyncio.sleep(3 * attempt)

        return []

    # ── navigate search tab ────────────────────────────────────────────────
    async def _select_search_tab(self, page, tab_name: str):
        try:
            # Try clicking a tab/link labeled tab_name
            tab = page.locator(f"text='{tab_name}'").first
            if await tab.count() > 0:
                await tab.click()
                await asyncio.sleep(0.5)
        except Exception:
            pass  # may not exist on all layouts

    # ── fill date fields ───────────────────────────────────────────────────
    async def _fill_date_range(self, page, start: str, end: str):
        # Maricopa recorder uses various field id patterns
        start_fmt = datetime.strptime(start, "%Y-%m-%d").strftime("%m/%d/%Y")
        end_fmt   = datetime.strptime(end,   "%Y-%m-%d").strftime("%m/%d/%Y")

        date_selectors = [
            ("#startDate", "#endDate"),
            ("#txtStartDate", "#txtEndDate"),
            ("input[name='startDate']", "input[name='endDate']"),
            ("input[placeholder*='Start']", "input[placeholder*='End']"),
            ("#dateFrom", "#dateTo"),
        ]
        filled = False
        for s_sel, e_sel in date_selectors:
            try:
                s_el = page.locator(s_sel).first
                e_el = page.locator(e_sel).first
                if await s_el.count() > 0 and await e_el.count() > 0:
                    await s_el.fill(start_fmt)
                    await e_el.fill(end_fmt)
                    filled = True
                    break
            except Exception:
                continue

        if not filled:
            log.warning("Could not locate date fields — using default range")

    # ── select document category ───────────────────────────────────────────
    async def _select_category(self, page, category: str):
        selectors = [
            "select#category",
            "select#docCategory",
            "select[name='category']",
            "select#ddlCategory",
        ]
        for sel in selectors:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    await el.select_option(label=category)
                    await asyncio.sleep(0.3)
                    return
            except Exception:
                continue

        # Try by value
        for sel in selectors:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    await el.select_option(value=category)
                    return
            except Exception:
                continue

    # ── select sub-type ────────────────────────────────────────────────────
    async def _select_doc_subtype(self, page, search_type: str):
        selectors = [
            "select#docType",
            "select#documentType",
            "select[name='docType']",
            "select#ddlDocType",
        ]
        for sel in selectors:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    # Try value first, then label
                    try:
                        await el.select_option(value=search_type)
                    except Exception:
                        await el.select_option(label=search_type)
                    await asyncio.sleep(0.3)
                    return
            except Exception:
                continue

    # ── submit search form ─────────────────────────────────────────────────
    async def _submit_search(self, page):
        submit_selectors = [
            "button[type='submit']",
            "input[type='submit']",
            "#btnSearch",
            "button#search",
            "button:has-text('Search')",
            "input[value='Search']",
        ]
        for sel in submit_selectors:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    await el.click()
                    await asyncio.sleep(1)
                    return
            except Exception:
                continue

    # ── parse paginated results ────────────────────────────────────────────
    async def _parse_all_pages(self, page, lead_key, cat, cat_label) -> list[dict]:
        all_records = []
        page_num = 1

        while True:
            log.debug(f"  Parsing page {page_num} for {lead_key}")
            recs = await self._parse_result_page(page, lead_key, cat, cat_label)
            all_records.extend(recs)

            # Try to go to next page
            went = await self._next_page(page)
            if not went:
                break
            page_num += 1
            await asyncio.sleep(1)

            if page_num > 50:   # safety cap
                log.warning(f"  Hit page cap (50) for {lead_key}")
                break

        return all_records

    # ── parse one result page ──────────────────────────────────────────────
    async def _parse_result_page(self, page, lead_key, cat, cat_label) -> list[dict]:
        records = []

        # Try multiple table selectors the recorder portal might use
        row_selectors = [
            "table.resultsTable tbody tr",
            "#tblResults tbody tr",
            "#searchResults tbody tr",
            "table tbody tr[onclick]",
            ".result-row",
        ]

        rows = None
        for sel in row_selectors:
            try:
                els = page.locator(sel)
                if await els.count() > 0:
                    rows = els
                    break
            except Exception:
                continue

        if rows is None:
            log.warning("  No result rows found on page")
            return []

        count = await rows.count()
        for i in range(count):
            try:
                rec = await self._parse_row(rows.nth(i), lead_key, cat, cat_label)
                if rec:
                    records.append(rec)
            except Exception as exc:
                log.debug(f"  Row {i} parse error: {exc}")
                continue

        return records

    # ── parse single row ───────────────────────────────────────────────────
    async def _parse_row(self, row, lead_key, cat, cat_label) -> Optional[dict]:
        cells = row.locator("td")
        cell_count = await cells.count()
        if cell_count < 3:
            return None

        texts = []
        for j in range(cell_count):
            try:
                t = (await cells.nth(j).inner_text()).strip()
                texts.append(t)
            except Exception:
                texts.append("")

        # Maricopa recorder columns (typical):
        # [0] Doc Number  [1] Recorded Date  [2] Doc Type  [3] Grantor  [4] Grantee  [5] Legal/Book  [6] Pages
        doc_num   = texts[0] if len(texts) > 0 else ""
        filed     = _norm_date(texts[1] if len(texts) > 1 else "")
        doc_type  = texts[2] if len(texts) > 2 else cat
        grantor   = texts[3] if len(texts) > 3 else ""
        grantee   = texts[4] if len(texts) > 4 else ""
        legal     = texts[5] if len(texts) > 5 else ""

        if not doc_num or not doc_num[0].isdigit():
            return None

        # Get the detail URL from onclick or <a>
        clerk_url = await self._get_detail_url(row, doc_num)

        # Parse amount from legal / tooltip if present
        amount = _extract_amount(legal + " " + " ".join(texts))

        rec = {
            "doc_num":    doc_num.strip(),
            "doc_type":   doc_type.strip() or cat,
            "filed":      filed,
            "cat":        cat,
            "cat_label":  cat_label,
            "lead_key":   lead_key,
            "owner":      grantor.strip(),
            "grantee":    grantee.strip(),
            "amount":     amount,
            "legal":      legal.strip(),
            # These will be filled by enricher
            "prop_address":  None,
            "prop_city":     None,
            "prop_state":    "AZ",
            "prop_zip":      None,
            "mail_address":  None,
            "mail_city":     None,
            "mail_state":    None,
            "mail_zip":      None,
            "parcel":        None,
            "trustee_name":  None,
            "trustee_phone": None,
            "auction_date":  None,
            "pdf_url":       None,
            "clerk_url":     clerk_url,
            "flags":         [],
            "score":         0,
        }
        return rec

    # ── extract detail URL from row ────────────────────────────────────────
    async def _get_detail_url(self, row, doc_num: str) -> str:
        try:
            link = row.locator("a").first
            if await link.count() > 0:
                href = await link.get_attribute("href")
                if href:
                    if href.startswith("http"):
                        return href
                    return BASE_URL + "/" + href.lstrip("/")
        except Exception:
            pass

        # Build URL from doc number pattern
        return f"{BASE_URL}/recording/document-detail.aspx?doc={doc_num}"

    # ── paginate ───────────────────────────────────────────────────────────
    async def _next_page(self, page) -> bool:
        next_selectors = [
            "a:has-text('Next')",
            "a:has-text('>')",
            "a.next",
            "#nextPage",
            "input[value='Next']",
            "a[title='Next Page']",
        ]
        for sel in next_selectors:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    disabled = await el.get_attribute("disabled")
                    cls      = await el.get_attribute("class") or ""
                    if disabled or "disabled" in cls:
                        return False
                    await el.click()
                    await asyncio.sleep(1.5)
                    return True
            except Exception:
                continue
        return False


# ── utility functions ──────────────────────────────────────────────────────────
def _norm_date(raw: str) -> str:
    """Normalize various date formats to YYYY-MM-DD."""
    raw = raw.strip()
    for fmt in ("%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw


def _extract_amount(text: str) -> Optional[float]:
    """Pull first dollar amount from text."""
    matches = re.findall(r"\$[\d,]+(?:\.\d{2})?", text)
    if matches:
        try:
            return float(matches[0].replace("$", "").replace(",", ""))
        except ValueError:
            pass
    return None
