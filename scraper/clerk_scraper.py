"""
scraper/clerk_scraper.py
Playwright-based async scraper for Maricopa County Recorder document search.
URL: https://recorder.maricopa.gov/recording/document-search.html

Fixes applied v3:
  - wait_until="domcontentloaded" instead of "networkidle" (portal never goes idle)
  - Replaced wait_for_function with a patient poll loop (avoids 30s hard timeout)
  - Increased page.goto timeout to 90s to handle slow government server
  - Added screenshot capture on failure for debugging
"""

import asyncio
import logging
import re
from datetime import datetime
from typing import Optional

log = logging.getLogger("clerk_scraper")

BASE_URL    = "https://recorder.maricopa.gov"
SEARCH_URL  = f"{BASE_URL}/recording/document-search.html"

MAX_RETRIES  = 3
GOTO_TIMEOUT = 90_000    # ms — government site can be very slow
PAGE_TIMEOUT = 60_000

# Exact codes for the Maricopa recorder autocomplete search box
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
        self.lead_types = lead_types
        self.start_date = start_date
        self.end_date   = end_date
        self.records: list[dict] = []

    # ── public entry point ─────────────────────────────────────────────────
    async def run(self) -> list[dict]:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
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
                doc_code       = DOC_CODES.get(lead_key)
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
                    log.error(f"  ✗ Failed {lead_key}: {exc}", exc_info=True)

                await asyncio.sleep(3)

            await browser.close()

        log.info(f"Total clerk records: {len(self.records)}")
        return self.records

    # ── scrape one document type ───────────────────────────────────────────
    async def _scrape_one_type(
        self, page, lead_key: str, doc_code: str, cat: str, cat_label: str
    ) -> list[dict]:

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                # ── KEY FIX: domcontentloaded NOT networkidle ──────────────
                # networkidle waits for all XHR to stop — Maricopa's site
                # has background requests that never fully settle, causing
                # the 60s goto timeout seen in the logs.
                log.debug(f"  Attempt {attempt}: navigating")
                await page.goto(
                    SEARCH_URL,
                    wait_until="domcontentloaded",
                    timeout=GOTO_TIMEOUT,
                )

                # Wait for JS form widgets to initialize
                await asyncio.sleep(3)

                # ── fill dates ─────────────────────────────────────────────
                await self._fill_date_field(page, 0, self.start_date)
                await asyncio.sleep(0.5)
                await self._fill_date_field(page, 1, self.end_date)
                await asyncio.sleep(0.5)

                # ── fill doc code ──────────────────────────────────────────
                await self._fill_doc_code(page, doc_code)
                await asyncio.sleep(0.5)

                # ── click SEARCH ───────────────────────────────────────────
                await self._click_search(page)

                # ── patient poll — no hard timeout exception ───────────────
                found = await self._wait_for_results(page, timeout_seconds=60)
                if not found:
                    log.warning(f"  Results never appeared for {lead_key} attempt {attempt}")
                    raise TimeoutError("Results did not load within 60s")

                # ── check for no-results message ───────────────────────────
                body_text = (await page.inner_text("body")).lower()
                if any(p in body_text for p in
                       ("no results", "no records", "0 results", "no matching")):
                    log.info(f"  No results for {lead_key}")
                    return []

                return await self._parse_all_pages(page, lead_key, cat, cat_label)

            except Exception as exc:
                log.warning(f"  Attempt {attempt}/{MAX_RETRIES} failed for {lead_key}: {exc}")
                try:
                    await page.screenshot(path=f"scraper/debug_{lead_key}_attempt{attempt}.png")
                    log.info(f"  Debug screenshot saved")
                except Exception:
                    pass

                if attempt == MAX_RETRIES:
                    log.error(f"  All retries exhausted for {lead_key}")
                    return []
                await asyncio.sleep(5 * attempt)

        return []

    # ── patient results poller ─────────────────────────────────────────────
    async def _wait_for_results(self, page, timeout_seconds: int = 60) -> bool:
        """
        Poll every 2 seconds instead of using a hard wait_for_function timeout.
        Returns True when results table OR a no-results message appears.
        """
        for _ in range(timeout_seconds // 2):
            await asyncio.sleep(2)
            try:
                if await page.locator("table tbody tr").count() > 0:
                    return True
                body = (await page.inner_text("body")).lower()
                if any(p in body for p in ("no results", "no records", "0 results")):
                    return True
                if await page.locator("[class*='result-count'], #totalResults").count() > 0:
                    return True
            except Exception:
                pass
        return False

    # ── fill date input by index (0=beginning, 1=end) ─────────────────────
    async def _fill_date_field(self, page, index: int, date_str: str):
        # Primary: input[type="date"]
        date_inputs = page.locator("input[type='date']")
        count = await date_inputs.count()
        log.debug(f"  Found {count} input[type='date'] field(s)")

        if count > index:
            success = await self._try_fill_date(date_inputs.nth(index), date_str)
            if success:
                log.debug(f"  ✓ Filled date[{index}] = {date_str}")
                return

        # Fallback: MM/DD/YYYY placeholder inputs
        for sel in ["input[placeholder*='MM/DD/YYYY']", "input[placeholder*='mm/dd']"]:
            els = page.locator(sel)
            if await els.count() > index:
                if await self._try_fill_date(els.nth(index), date_str):
                    log.debug(f"  ✓ Filled date[{index}] via placeholder fallback")
                    return

        log.warning(f"  Could not fill date field [{index}] — proceeding without it")

    async def _try_fill_date(self, locator, date_str: str) -> bool:
        mm_dd_yyyy = datetime.strptime(date_str, "%Y-%m-%d").strftime("%m/%d/%Y")

        # 1. Direct fill YYYY-MM-DD (native date input)
        try:
            await locator.fill(date_str)
            await asyncio.sleep(0.2)
            if await locator.input_value():
                return True
        except Exception:
            pass

        # 2. Triple-click + type MM/DD/YYYY
        try:
            await locator.triple_click()
            await locator.type(mm_dd_yyyy, delay=40)
            await asyncio.sleep(0.2)
            if await locator.input_value():
                return True
        except Exception:
            pass

        # 3. JavaScript set value + fire events
        try:
            await locator.evaluate(
                """(el, v) => {
                    el.value = v;
                    el.dispatchEvent(new Event('input',  {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                }""",
                date_str,
            )
            return True
        except Exception:
            pass

        return False

    # ── fill document code autocomplete ───────────────────────────────────
    async def _fill_doc_code(self, page, doc_code: str):
        field = None
        for sel in [
            "input[placeholder*='search for document codes']",
            "input[placeholder*='document code']",
            "input[placeholder*='Type to search']",
            "input[placeholder*='document']",
        ]:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    field = el
                    break
            except Exception:
                continue

        if field is None:
            log.warning("  Doc code field not found — will search all codes")
            return

        await field.click()
        await asyncio.sleep(0.3)
        await field.fill("")
        await field.type(doc_code, delay=80)
        await asyncio.sleep(1.5)

        # Click first autocomplete suggestion
        for sel in [
            f"li:has-text('{doc_code}')",
            f"[role='option']:has-text('{doc_code}')",
            "[class*='autocomplete'] li",
            "[class*='suggest'] li",
            ".ui-autocomplete li",
        ]:
            try:
                opts = page.locator(sel)
                if await opts.count() > 0:
                    await opts.first.click()
                    log.debug(f"  Selected '{doc_code}' from dropdown")
                    await asyncio.sleep(0.5)
                    return
            except Exception:
                continue

        await field.press("Tab")
        log.debug(f"  No dropdown; pressed Tab after '{doc_code}'")

    # ── click SEARCH button ────────────────────────────────────────────────
    async def _click_search(self, page):
        for sel in [
            "button:has-text('SEARCH')",
            "button:has-text('Search')",
            "input[value='SEARCH']",
            "input[value='Search']",
            "button[type='submit']",
            "input[type='submit']",
        ]:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    await el.click()
                    log.debug(f"  Clicked search via: {sel}")
                    await asyncio.sleep(2)
                    return
            except Exception:
                continue
        log.warning("  SEARCH button not found — pressing Enter")
        await page.keyboard.press("Enter")

    # ── parse all result pages ─────────────────────────────────────────────
    async def _parse_all_pages(self, page, lead_key, cat, cat_label) -> list[dict]:
        all_records = []
        page_num    = 1
        while True:
            recs = await self._parse_result_page(page, lead_key, cat, cat_label)
            all_records.extend(recs)
            log.debug(f"  Page {page_num}: {len(recs)} rows")
            if not await self._go_next_page(page):
                break
            page_num += 1
            await asyncio.sleep(2)
            if page_num > 100:
                log.warning(f"  Page cap (100) hit for {lead_key}")
                break
        return all_records

    # ── parse one result page ──────────────────────────────────────────────
    async def _parse_result_page(self, page, lead_key, cat, cat_label) -> list[dict]:
        records = []
        rows    = None
        for sel in ["table tbody tr", "table tr:not(:first-child)", "[class*='result'] tr"]:
            try:
                els = page.locator(sel)
                if await els.count() > 0:
                    rows = els
                    break
            except Exception:
                continue

        if rows is None:
            log.warning("  No result rows found")
            return []

        for i in range(await rows.count()):
            try:
                rec = await self._parse_row(rows.nth(i), lead_key, cat, cat_label)
                if rec:
                    records.append(rec)
            except Exception as exc:
                log.debug(f"  Row {i} error: {exc}")
        return records

    # ── parse a single row ─────────────────────────────────────────────────
    async def _parse_row(self, row, lead_key, cat, cat_label) -> Optional[dict]:
        cells = row.locator("td")
        if await cells.count() < 2:
            return None

        texts = []
        for j in range(await cells.count()):
            try:
                texts.append((await cells.nth(j).inner_text()).strip())
            except Exception:
                texts.append("")

        doc_num  = texts[0] if texts else ""
        filed    = _norm_date(texts[1]) if len(texts) > 1 else ""
        doc_type = texts[2] if len(texts) > 2 else cat
        grantor  = texts[3] if len(texts) > 3 else ""
        grantee  = texts[4] if len(texts) > 4 else ""
        legal    = texts[5] if len(texts) > 5 else ""

        if not doc_num or not doc_num[0].isdigit():
            return None
        if doc_num.lower() in ("doc number", "document number", "#"):
            return None

        return {
            "doc_num":       doc_num.strip(),
            "doc_type":      doc_type.strip() or cat,
            "filed":         filed,
            "cat":           cat,
            "cat_label":     cat_label,
            "lead_key":      lead_key,
            "owner":         grantor.strip(),
            "grantee":       grantee.strip(),
            "amount":        _extract_amount(" ".join(texts)),
            "legal":         legal.strip(),
            "prop_address":  None, "prop_city":    None,
            "prop_state":    "AZ", "prop_zip":     None,
            "mail_address":  None, "mail_city":    None,
            "mail_state":    None, "mail_zip":     None,
            "parcel":        None,
            "first_name":    None, "last_name":    None,
            "first_name_2":  None, "last_name_2":  None,
            "trustee_name":  None, "trustee_phone": None,
            "auction_date":  None, "pdf_url":       None,
            "clerk_url":     await self._get_detail_url(row, doc_num),
            "flags":         [], "score": 0,
        }

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

    async def _go_next_page(self, page) -> bool:
        for sel in ["a:has-text('Next')", "button:has-text('Next')", "a[aria-label='Next page']"]:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    disabled = await el.get_attribute("disabled")
                    cls      = await el.get_attribute("class") or ""
                    if disabled or "disabled" in cls:
                        return False
                    await el.click()
                    await asyncio.sleep(2)
                    return True
            except Exception:
                continue
        return False


# ── utilities ──────────────────────────────────────────────────────────────────
def _norm_date(raw: str) -> str:
    for fmt in ("%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(raw.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw.strip()


def _extract_amount(text: str) -> Optional[float]:
    m = re.findall(r"\$[\d,]+(?:\.\d{2})?", text)
    if m:
        try:
            return float(m[0].replace("$", "").replace(",", ""))
        except ValueError:
            pass
    return None
