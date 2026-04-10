"""
scraper/clerk_scraper.py  v4
Maricopa County Recorder document search scraper.

This version has two key improvements:

1. API DISCOVERY MODE (first run):
   Loads the page, waits properly for JS form, dumps ALL input element
   attributes to the log, captures the actual API network call made when
   Search is clicked. This tells us the exact field names and endpoint URL.

2. JS-INJECTION FORM FILLING (fallback):
   Instead of guessing CSS selectors, we use JavaScript to scan ALL inputs
   and fill whichever ones match date/doccode patterns — works regardless
   of the actual id/name/class attributes used.

The logs from the next run will contain:
  "FORM INPUTS FOUND" — the exact id, name, placeholder of every input
  "Network: POST <url>" — the API endpoint we can call directly next time
  "debug_form.html" — committed to repo so you can inspect the full HTML
"""

import asyncio
import json
import logging
import re
import time
from datetime import datetime
from typing import Optional

import requests

log = logging.getLogger("clerk_scraper")

BASE_URL   = "https://recorder.maricopa.gov"
SEARCH_URL = f"{BASE_URL}/recording/document-search.html"

MAX_RETRIES  = 3
GOTO_TIMEOUT = 120_000
PAGE_TIMEOUT = 90_000

DOC_CODES = {
    "NS": "NOTS",
    "FL": "FTLF",
    "SL": "STLF",
    "DE": "TDEED",
    "PD": "PROBD",
    "PJ": "PROJD",
}

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
})


class ClerkScraper:
    def __init__(self, lead_types: dict, start_date: str, end_date: str):
        self.lead_types  = lead_types
        self.start_date  = start_date
        self.end_date    = end_date
        self.records: list[dict] = []
        self._api_endpoint: Optional[str] = None
        self._api_payload_template: Optional[dict] = None

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

            # ── Step 1: discover the form structure ────────────────────────
            await self._discover_form(ctx)

            # ── Step 2: scrape each doc type ───────────────────────────────
            for lead_key in self.lead_types:
                doc_code       = DOC_CODES.get(lead_key)
                cat, cat_label = self.lead_types[lead_key]
                log.info(f"Scraping {lead_key} ({cat_label}) — code: {doc_code}")

                if not doc_code:
                    continue

                page = await ctx.new_page()
                page.set_default_timeout(PAGE_TIMEOUT)

                try:
                    recs = await self._scrape_one(page, lead_key, doc_code, cat, cat_label)
                    log.info(f"  → {len(recs)} records for {lead_key}")
                    self.records.extend(recs)
                except Exception as exc:
                    log.error(f"  ✗ Failed {lead_key}: {exc}", exc_info=True)
                finally:
                    await page.close()

                await asyncio.sleep(3)

            await browser.close()

        log.info(f"Total records: {len(self.records)}")
        return self.records

    # ── discover form structure on first page load ─────────────────────────
    async def _discover_form(self, ctx):
        """
        Load the page, wait for JS to render, log everything we see.
        This gives us the exact information needed to fix selectors.
        """
        page = await ctx.new_page()
        captured_requests = []

        async def on_request(req):
            url = req.url
            if any(k in url.lower() for k in ("search", "doc", "record", "api", "query")):
                try:
                    captured_requests.append({
                        "method": req.method,
                        "url": url,
                        "post": req.post_data or "",
                    })
                except Exception:
                    pass

        page.on("request", on_request)

        try:
            log.info("DISCOVERY: Loading search page…")
            await page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT)

            # Poll for any input to appear
            log.info("DISCOVERY: Waiting for any inputs to appear in DOM…")
            appeared = False
            for i in range(60):   # wait up to 60 seconds
                await asyncio.sleep(1)
                try:
                    n = await page.locator("input").count()
                    if n > 0:
                        log.info(f"DISCOVERY: {n} input(s) appeared after {i+1}s")
                        appeared = True
                        break
                except Exception:
                    pass

            if not appeared:
                log.warning("DISCOVERY: No inputs appeared in 60s!")

            # ── dump EVERY input's attributes ──────────────────────────────
            inputs_data = await page.evaluate("""
                () => {
                    const result = [];
                    document.querySelectorAll('input, select, button, textarea').forEach(el => {
                        result.push({
                            tag:         el.tagName,
                            type:        el.type || '',
                            id:          el.id || '',
                            name:        el.name || '',
                            placeholder: el.placeholder || '',
                            class:       el.className.substring(0, 80) || '',
                            value:       el.value || '',
                            visible:     el.offsetParent !== null,
                            aria_label:  el.getAttribute('aria-label') || '',
                        });
                    });
                    return result;
                }
            """)

            log.info(f"DISCOVERY: ========== ALL FORM ELEMENTS ({len(inputs_data)}) ==========")
            for el in inputs_data:
                log.info(f"DISCOVERY:   {el}")
            log.info("DISCOVERY: ================================================")

            # ── save full page HTML ────────────────────────────────────────
            try:
                full_html = await page.content()
                with open("scraper/debug_page.html", "w", encoding="utf-8") as f:
                    f.write(full_html)
                log.info("DISCOVERY: Full page HTML → scraper/debug_page.html")
            except Exception as e:
                log.warning(f"DISCOVERY: Could not save page HTML: {e}")

            # ── take a screenshot ──────────────────────────────────────────
            try:
                await page.screenshot(
                    path="scraper/debug_page.png", full_page=True
                )
                log.info("DISCOVERY: Screenshot → scraper/debug_page.png")
            except Exception as e:
                log.warning(f"DISCOVERY: Screenshot failed: {e}")

            # ── try submitting and capture the API call ────────────────────
            if appeared:
                log.info("DISCOVERY: Attempting test search submission…")
                await self._js_fill_all(page, "NOTS", self.start_date, self.end_date)
                await asyncio.sleep(0.5)
                await self._js_click_search(page)
                await asyncio.sleep(5)

                log.info(f"DISCOVERY: Network requests captured ({len(captured_requests)}):")
                for req in captured_requests:
                    log.info(f"DISCOVERY:   {req['method']} {req['url']}")
                    if req['post']:
                        log.info(f"DISCOVERY:   POST DATA: {req['post'][:300]}")

                # Save a post-search screenshot
                try:
                    await page.screenshot(path="scraper/debug_after_search.png", full_page=True)
                    log.info("DISCOVERY: Post-search screenshot → scraper/debug_after_search.png")
                except Exception:
                    pass

        except Exception as exc:
            log.error(f"DISCOVERY: Failed: {exc}", exc_info=True)
        finally:
            await page.close()

    # ── scrape one doc type ────────────────────────────────────────────────
    async def _scrape_one(self, page, lead_key, doc_code, cat, cat_label) -> list[dict]:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                await page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT)

                # Wait for inputs
                for i in range(45):
                    await asyncio.sleep(1)
                    try:
                        if await page.locator("input").count() > 0:
                            break
                    except Exception:
                        pass

                await self._js_fill_all(page, doc_code, self.start_date, self.end_date)
                await asyncio.sleep(0.5)
                await self._js_click_search(page)

                found = await self._poll_for_results(page, 60)
                if not found:
                    await page.screenshot(path=f"scraper/debug_{lead_key}_a{attempt}.png")
                    raise TimeoutError("No results appeared")

                body = (await page.inner_text("body")).lower()
                if any(p in body for p in ("no results", "no records", "0 results")):
                    return []

                return await self._parse_all_pages(page, lead_key, cat, cat_label)

            except Exception as exc:
                log.warning(f"  Attempt {attempt}/{MAX_RETRIES}: {exc}")
                if attempt == MAX_RETRIES:
                    return []
                await asyncio.sleep(5 * attempt)

        return []

    # ── fill all form fields via JavaScript ────────────────────────────────
    async def _js_fill_all(self, page, doc_code: str, start: str, end: str):
        start_fmt = datetime.strptime(start, "%Y-%m-%d").strftime("%m/%d/%Y")
        end_fmt   = datetime.strptime(end,   "%Y-%m-%d").strftime("%m/%d/%Y")

        result = await page.evaluate(f"""
            () => {{
                const log = [];
                const all = Array.from(document.querySelectorAll('input'));

                // ── DATE FIELDS ──────────────────────────────────────────
                const dateByType = all.filter(i => i.type === 'date');
                if (dateByType.length >= 2) {{
                    dateByType[0].value = '{start}';
                    dateByType[1].value = '{end}';
                    dateByType.forEach(i => {{
                        i.dispatchEvent(new Event('input',  {{bubbles:true}}));
                        i.dispatchEvent(new Event('change', {{bubbles:true}}));
                    }});
                    log.push('date-by-type:' + dateByType.length);
                }}

                const mmdd = all.filter(i =>
                    i.placeholder && i.placeholder.toUpperCase().includes('MM/DD')
                );
                if (mmdd.length >= 2) {{
                    mmdd[0].value = '{start_fmt}';
                    mmdd[1].value = '{end_fmt}';
                    mmdd.forEach(i => {{
                        i.dispatchEvent(new Event('input',  {{bubbles:true}}));
                        i.dispatchEvent(new Event('change', {{bubbles:true}}));
                    }});
                    log.push('mmdd-placeholder:' + mmdd.length);
                }}

                // Named begin/end
                const named = [
                    ['begin', 'start'],
                    ['end', 'thru', 'through'],
                ];
                const dateValues = [
                    '{start}', '{start_fmt}',
                    '{end}',   '{end_fmt}',
                ];
                // Try common id/name patterns
                const pairs = [
                    ['beginDate', 'endDate'],
                    ['startDate', 'endDate'],
                    ['fromDate',  'toDate'],
                    ['dateFrom',  'dateTo'],
                    ['txtBegin',  'txtEnd'],
                ];
                for (const [sid, eid] of pairs) {{
                    const s = document.getElementById(sid) ||
                              document.querySelector('[name="' + sid + '"]');
                    const e = document.getElementById(eid) ||
                              document.querySelector('[name="' + eid + '"]');
                    if (s && e) {{
                        s.value = s.type === 'date' ? '{start}' : '{start_fmt}';
                        e.value = e.type === 'date' ? '{end}'   : '{end_fmt}';
                        s.dispatchEvent(new Event('change', {{bubbles:true}}));
                        e.dispatchEvent(new Event('change', {{bubbles:true}}));
                        log.push('named-pair:' + sid + '/' + eid);
                        break;
                    }}
                }}

                // ── DOC CODE FIELD ───────────────────────────────────────
                const docField = all.find(i =>
                    (i.placeholder && (
                        i.placeholder.toLowerCase().includes('document code') ||
                        i.placeholder.toLowerCase().includes('type to search') ||
                        i.placeholder.toLowerCase().includes('search for doc')
                    )) ||
                    (i.id   && i.id.toLowerCase().includes('doccode')) ||
                    (i.name && i.name.toLowerCase().includes('doccode')) ||
                    (i.id   && i.id.toLowerCase().includes('doc_code')) ||
                    (i.id   && i.id.toLowerCase().includes('documentcode'))
                );
                if (docField) {{
                    docField.focus();
                    docField.value = '{doc_code}';
                    docField.dispatchEvent(new Event('input',  {{bubbles:true}}));
                    docField.dispatchEvent(new Event('change', {{bubbles:true}}));
                    docField.dispatchEvent(new KeyboardEvent('keyup', {{bubbles:true}}));
                    log.push('doccode:' + (docField.id || docField.placeholder));
                }} else {{
                    log.push('doccode-field-not-found');
                }}

                return log;
            }}
        """)
        log.info(f"  JS fill result: {result}")

        # After triggering input event on doc code, wait and click first autocomplete option
        await asyncio.sleep(1.5)
        clicked = await page.evaluate(f"""
            () => {{
                const candidates = document.querySelectorAll(
                    'li, [role="option"], [class*="autocomplete"] *, [class*="suggest"] *'
                );
                for (const el of candidates) {{
                    if (el.textContent.trim().toUpperCase().includes('{doc_code}')) {{
                        el.click();
                        return el.textContent.trim().substring(0, 50);
                    }}
                }}
                // Click first visible dropdown item if any
                const dropItems = document.querySelectorAll(
                    '[class*="dropdown"] li, [class*="auto"] li, ul li'
                );
                if (dropItems.length > 0) {{
                    dropItems[0].click();
                    return 'first-item: ' + dropItems[0].textContent.trim().substring(0, 50);
                }}
                return null;
            }}
        """)
        if clicked:
            log.info(f"  Autocomplete selection: '{clicked}'")

    async def _js_click_search(self, page):
        result = await page.evaluate("""
            () => {
                const all = Array.from(
                    document.querySelectorAll('button, input[type="submit"], a')
                );
                const btn = all.find(el => {
                    const txt = (el.textContent || el.value || '').toUpperCase().trim();
                    return txt === 'SEARCH' || txt.startsWith('SEARCH');
                });
                if (btn) {
                    btn.click();
                    return btn.textContent.trim() || btn.value;
                }
                return null;
            }
        """)
        if result:
            log.info(f"  Search clicked: '{result}'")
        else:
            log.warning("  Search button not found via JS — pressing Enter")
            await page.keyboard.press("Enter")
        await asyncio.sleep(2)

    async def _poll_for_results(self, page, timeout_seconds=60) -> bool:
        for _ in range(timeout_seconds // 2):
            await asyncio.sleep(2)
            try:
                if await page.locator("table tbody tr").count() > 0:
                    return True
                body = (await page.inner_text("body")).lower()
                if any(p in body for p in ("no results", "no records", "0 results", "0 matching")):
                    return True
            except Exception:
                pass
        return False

    async def _parse_all_pages(self, page, lead_key, cat, cat_label) -> list[dict]:
        all_records = []
        page_num    = 1
        while True:
            recs = await self._parse_result_page(page, lead_key, cat, cat_label)
            all_records.extend(recs)
            if not await self._go_next_page(page):
                break
            page_num += 1
            await asyncio.sleep(2)
            if page_num > 100:
                break
        return all_records

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
            return []
        for i in range(await rows.count()):
            try:
                rec = await self._parse_row(rows.nth(i), lead_key, cat, cat_label)
                if rec:
                    records.append(rec)
            except Exception:
                pass
        return records

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
        doc_num = texts[0] if texts else ""
        if not doc_num or not doc_num[0].isdigit():
            return None
        return {
            "doc_num":       doc_num,
            "doc_type":      texts[2] if len(texts) > 2 else cat,
            "filed":         _norm_date(texts[1]) if len(texts) > 1 else "",
            "cat":           cat,
            "cat_label":     cat_label,
            "lead_key":      lead_key,
            "owner":         texts[3] if len(texts) > 3 else "",
            "grantee":       texts[4] if len(texts) > 4 else "",
            "amount":        _extract_amount(" ".join(texts)),
            "legal":         texts[5] if len(texts) > 5 else "",
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
            "flags": [], "score": 0,
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
                    cls = await el.get_attribute("class") or ""
                    if "disabled" in cls or await el.get_attribute("disabled"):
                        return False
                    await el.click()
                    await asyncio.sleep(2)
                    return True
            except Exception:
                continue
        return False


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
