"""
scraper/clerk_scraper.py
Playwright-based async scraper for Maricopa County Recorder document search.
URL: https://recorder.maricopa.gov/recording/document-search.html

Form layout (confirmed from live site inspection):
  - BEGINNING DATE  → input[type="date"] first instance
  - END DATE        → input[type="date"] second instance
  - DOCUMENT CODE   → text input autocomplete ("Type to search for document codes")
  - SEARCH button   → button/input with text "SEARCH"
  - Results table   → rendered after SEARCH click
"""

import asyncio
import logging
import re
import time
from datetime import datetime
from typing import Optional

log = logging.getLogger("clerk_scraper")

BASE_URL   = "https://recorder.maricopa.gov"
SEARCH_URL = f"{BASE_URL}/recording/document-search.html"

MAX_RETRIES  = 3
PAGE_TIMEOUT = 45_000   # ms — portal can be slow
NAV_TIMEOUT  = 60_000

# ── Document codes used in the Maricopa recorder autocomplete ─────────────────
# These are the exact codes typed into "Type to search for document codes"
# The portal accepts partial matches and shows a dropdown to select from.
DOC_CODES = {
    "NS": "NOTS",   # Notice of Trustee Sale
    "FL": "FTLF",   # Federal Tax Lien Filed
    "SL": "STLF",   # State Tax Lien Filed
    "DE": "TDEED",  # Tax Deed
    "PD": "PROBD",  # Probate — Deceased
    "PJ": "PROJD",  # Probate — Judgment
}


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
                ),
                viewport={"width": 1280, "height": 900},
            )
            page = await ctx.new_page()
            page.set_default_timeout(PAGE_TIMEOUT)

            for lead_key in self.lead_types:
                doc_code  = DOC_CODES.get(lead_key)
                cat, cat_label = self.lead_types[lead_key]
                log.info(f"Scraping {lead_key} ({cat_label}) — code: {doc_code}")

                if not doc_code:
                    log.warning(f"No doc code mapped for {lead_key} — skipping")
                    continue

                try:
                    recs = await self._scrape_one_type(page, lead_key, doc_code, cat, cat_label)
                    log.info(f"  → {len(recs)} records for {lead_key}")
                    self.records.extend(recs)
                except Exception as exc:
                    log.error(f"  ✗ Failed scraping {lead_key}: {exc}", exc_info=True)

                # Small pause between searches to be polite
                await asyncio.sleep(2)

            await browser.close()

        log.info(f"Total clerk records collected: {len(self.records)}")
        return self.records

    # ── scrape one document type ───────────────────────────────────────────
    async def _scrape_one_type(
        self, page, lead_key: str, doc_code: str, cat: str, cat_label: str
    ) -> list[dict]:

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                # ── navigate fresh each attempt ────────────────────────────
                log.debug(f"  Attempt {attempt}: loading {SEARCH_URL}")
                await page.goto(SEARCH_URL, wait_until="networkidle", timeout=NAV_TIMEOUT)
                await asyncio.sleep(1.5)

                # ── scroll down to reveal the date + doc code fields ───────
                # The form has name fields at top; dates and doc code are below
                await page.evaluate("window.scrollTo(0, 400)")
                await asyncio.sleep(0.5)

                # ── fill BEGINNING DATE ────────────────────────────────────
                await self._fill_date_field(page, 0, self.start_date)

                # ── fill END DATE ──────────────────────────────────────────
                await self._fill_date_field(page, 1, self.end_date)

                # ── enter document code in autocomplete ────────────────────
                await self._fill_doc_code(page, doc_code)

                # ── click SEARCH ───────────────────────────────────────────
                await self._click_search(page)

                # ── wait for results or no-results indicator ───────────────
                log.debug("  Waiting for results…")
                try:
                    await page.wait_for_function(
                        """() => {
                            const tables = document.querySelectorAll('table');
                            const noRes  = document.body.innerText;
                            return tables.length > 0 ||
                                   noRes.includes('No results') ||
                                   noRes.includes('no records') ||
                                   noRes.includes('0 results');
                        }""",
                        timeout=30_000,
                    )
                except Exception:
                    log.warning(f"  Wait for results timed out on attempt {attempt}")
                    raise

                await asyncio.sleep(1)

                # ── check page text for no-results ─────────────────────────
                body_text = await page.inner_text("body")
                if any(phrase in body_text.lower() for phrase in
                       ("no results", "no records found", "0 results", "no matching")):
                    log.info(f"  No results found for {lead_key}")
                    return []

                # ── parse all result pages ─────────────────────────────────
                records = await self._parse_all_pages(page, lead_key, cat, cat_label)
                return records

            except Exception as exc:
                log.warning(f"  Attempt {attempt}/{MAX_RETRIES} failed for {lead_key}: {exc}")
                if attempt == MAX_RETRIES:
                    log.error(f"  All retries exhausted for {lead_key}")
                    return []
                await asyncio.sleep(4 * attempt)

        return []

    # ── fill a date input[type="date"] by index (0=start, 1=end) ──────────
    async def _fill_date_field(self, page, index: int, date_str: str):
        """
        The Maricopa form has two input[type='date'] fields.
        We target them by index. date_str is YYYY-MM-DD.
        Native date inputs accept YYYY-MM-DD via fill(), or MM/DD/YYYY via typing.
        """
        selector = f"input[type='date']"
        fields   = page.locator(selector)
        count    = await fields.count()
        log.debug(f"  Found {count} date input(s)")

        if count == 0:
            # Fallback: look for placeholder text
            fallback_selectors = [
                "input[placeholder*='MM/DD/YYYY']",
                "input[placeholder*='mm/dd/yyyy']",
                "#beginDate", "#endDate",
                "input[name*='begin']", "input[name*='end']",
                "input[name*='start']",
            ]
            for sel in fallback_selectors:
                try:
                    els = page.locator(sel)
                    c   = await els.count()
                    if c > index:
                        await self._type_date(els.nth(index), date_str)
                        log.debug(f"  Filled date via fallback selector {sel}[{index}]")
                        return
                except Exception:
                    continue
            log.warning(f"  Could not find date field at index {index}")
            return

        target = fields.nth(index) if count > index else fields.first
        await self._type_date(target, date_str)
        log.debug(f"  Filled date field [{index}] = {date_str}")

    async def _type_date(self, locator, date_str: str):
        """
        Fill a date input. Try direct fill (YYYY-MM-DD), then
        triple-click + type as MM/DD/YYYY if needed.
        """
        try:
            await locator.fill(date_str)
            await asyncio.sleep(0.2)
            val = await locator.input_value()
            if val and val != "":
                return
        except Exception:
            pass

        # Fallback: click to focus, clear, type MM/DD/YYYY
        mm_dd_yyyy = datetime.strptime(date_str, "%Y-%m-%d").strftime("%m/%d/%Y")
        try:
            await locator.triple_click()
            await locator.type(mm_dd_yyyy, delay=50)
            await asyncio.sleep(0.2)
        except Exception as e:
            log.warning(f"  Date type fallback failed: {e}")

        # Last resort: JavaScript set value
        try:
            await locator.evaluate(
                f"el => {{ el.value = '{date_str}'; el.dispatchEvent(new Event('change', {{bubbles:true}})); }}"
            )
        except Exception:
            pass

    # ── fill the document code autocomplete ───────────────────────────────
    async def _fill_doc_code(self, page, doc_code: str):
        """
        The form has: <input placeholder="Type to search for document codes">
        Typing triggers an autocomplete dropdown. We type the code and
        select the first matching option.
        """
        selectors = [
            "input[placeholder*='search for document codes']",
            "input[placeholder*='document code']",
            "input[placeholder*='Type to search']",
        ]

        field = None
        for sel in selectors:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    field = el
                    break
            except Exception:
                continue

        if field is None:
            log.warning(f"  Could not find doc code input — will search all codes")
            return

        # Click, clear, type the code
        await field.click()
        await asyncio.sleep(0.3)
        await field.fill("")
        await field.type(doc_code, delay=80)
        await asyncio.sleep(1.2)   # wait for autocomplete dropdown

        # Look for a dropdown suggestion and click it
        dropdown_selectors = [
            f"li:has-text('{doc_code}')",
            f"div.autocomplete-item:has-text('{doc_code}')",
            f"[role='option']:has-text('{doc_code}')",
            f"ul li:has-text('{doc_code}')",
            ".suggestions li",
            ".autocomplete-results li",
            "[class*='dropdown'] li",
            "[class*='suggest'] li",
        ]
        selected = False
        for sel in dropdown_selectors:
            try:
                opts = page.locator(sel)
                cnt  = await opts.count()
                if cnt > 0:
                    await opts.first.click()
                    selected = True
                    log.debug(f"  Selected doc code from dropdown: {doc_code}")
                    await asyncio.sleep(0.4)
                    break
            except Exception:
                continue

        if not selected:
            # Try pressing Enter to accept whatever is in the field
            await field.press("Enter")
            await asyncio.sleep(0.3)
            log.debug(f"  Pressed Enter to confirm doc code: {doc_code}")

    # ── click the SEARCH button ────────────────────────────────────────────
    async def _click_search(self, page):
        selectors = [
            "button:has-text('SEARCH')",
            "button:has-text('Search')",
            "input[value='SEARCH']",
            "input[value='Search']",
            "button[type='submit']",
            "input[type='submit']",
            "button.search-btn",
            "[class*='search'] button",
        ]
        for sel in selectors:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    await el.click()
                    log.debug(f"  Clicked search via: {sel}")
                    await asyncio.sleep(1.5)
                    return
            except Exception:
                continue
        log.warning("  Could not find SEARCH button — pressing Enter instead")
        await page.keyboard.press("Enter")

    # ── parse paginated results ────────────────────────────────────────────
    async def _parse_all_pages(self, page, lead_key, cat, cat_label) -> list[dict]:
        all_records = []
        page_num    = 1

        while True:
            log.debug(f"  Parsing result page {page_num} for {lead_key}")
            recs = await self._parse_result_page(page, lead_key, cat, cat_label)
            all_records.extend(recs)
            log.debug(f"  Page {page_num}: {len(recs)} rows")

            went = await self._go_next_page(page)
            if not went:
                break
            page_num += 1
            await asyncio.sleep(1.5)

            if page_num > 100:
                log.warning(f"  Hit page cap (100) for {lead_key}")
                break

        return all_records

    # ── parse one results page ─────────────────────────────────────────────
    async def _parse_result_page(self, page, lead_key, cat, cat_label) -> list[dict]:
        records = []

        # Try various table/row selectors the recorder might render
        row_selectors = [
            "table tbody tr",
            "table tr:not(:first-child)",   # skip header row
            ".results-table tbody tr",
            "[class*='result'] tr",
        ]

        rows = None
        for sel in row_selectors:
            try:
                els = page.locator(sel)
                cnt = await els.count()
                if cnt > 0:
                    rows = els
                    log.debug(f"  Found {cnt} rows via '{sel}'")
                    break
            except Exception:
                continue

        if rows is None:
            log.warning("  No result rows found — dumping visible text for debug")
            try:
                snippet = (await page.inner_text("body"))[:500]
                log.debug(f"  Body snippet: {snippet}")
            except Exception:
                pass
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

    # ── parse a single result row ──────────────────────────────────────────
    async def _parse_row(self, row, lead_key, cat, cat_label) -> Optional[dict]:
        cells      = row.locator("td")
        cell_count = await cells.count()
        if cell_count < 2:
            return None

        texts = []
        for j in range(cell_count):
            try:
                t = (await cells.nth(j).inner_text()).strip()
                texts.append(t)
            except Exception:
                texts.append("")

        # Maricopa recorder results columns (typical order):
        # Doc Number | Recorded Date | Doc Type | Grantor | Grantee | Legal | Pages
        doc_num  = texts[0] if len(texts) > 0 else ""
        filed    = _norm_date(texts[1]) if len(texts) > 1 else ""
        doc_type = texts[2] if len(texts) > 2 else cat
        grantor  = texts[3] if len(texts) > 3 else ""
        grantee  = texts[4] if len(texts) > 4 else ""
        legal    = texts[5] if len(texts) > 5 else ""

        # Skip header rows or empty rows
        if not doc_num or doc_num.lower() in ("doc number", "document number", "#", ""):
            return None
        if not doc_num[0].isdigit():
            return None

        clerk_url = await self._get_detail_url(row, doc_num)
        amount    = _extract_amount(" ".join(texts))

        return {
            "doc_num":       doc_num.strip(),
            "doc_type":      doc_type.strip() or cat,
            "filed":         filed,
            "cat":           cat,
            "cat_label":     cat_label,
            "lead_key":      lead_key,
            "owner":         grantor.strip(),
            "grantee":       grantee.strip(),
            "amount":        amount,
            "legal":         legal.strip(),
            "prop_address":  None,
            "prop_city":     None,
            "prop_state":    "AZ",
            "prop_zip":      None,
            "mail_address":  None,
            "mail_city":     None,
            "mail_state":    None,
            "mail_zip":      None,
            "parcel":        None,
            "first_name":    None,
            "last_name":     None,
            "first_name_2":  None,
            "last_name_2":   None,
            "trustee_name":  None,
            "trustee_phone": None,
            "auction_date":  None,
            "pdf_url":       None,
            "clerk_url":     clerk_url,
            "flags":         [],
            "score":         0,
        }

    # ── get detail URL from row ────────────────────────────────────────────
    async def _get_detail_url(self, row, doc_num: str) -> str:
        try:
            link = row.locator("a").first
            if await link.count() > 0:
                href = await link.get_attribute("href")
                if href:
                    return href if href.startswith("http") else BASE_URL + "/" + href.lstrip("/")
        except Exception:
            pass
        return f"{BASE_URL}/recording/document-detail.aspx?doc={doc_num}"

    # ── paginate ───────────────────────────────────────────────────────────
    async def _go_next_page(self, page) -> bool:
        selectors = [
            "a:has-text('Next')",
            "button:has-text('Next')",
            "a:has-text('>')",
            "a[aria-label='Next page']",
            "a[title='Next']",
            ".pagination a:last-child",
            "[class*='next']:not([disabled])",
        ]
        for sel in selectors:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    disabled = await el.get_attribute("disabled")
                    cls      = await el.get_attribute("class") or ""
                    aria     = await el.get_attribute("aria-disabled") or ""
                    if disabled or "disabled" in cls or aria == "true":
                        return False
                    await el.click()
                    await asyncio.sleep(2)
                    return True
            except Exception:
                continue
        return False


# ── utilities ──────────────────────────────────────────────────────────────────
def _norm_date(raw: str) -> str:
    raw = raw.strip()
    for fmt in ("%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw


def _extract_amount(text: str) -> Optional[float]:
    matches = re.findall(r"\$[\d,]+(?:\.\d{2})?", text)
    if matches:
        try:
            return float(matches[0].replace("$", "").replace(",", ""))
        except ValueError:
            pass
    return None
