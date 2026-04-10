"""
scraper/enricher.py  v4 — CONFIRMED WORKING

Strategy confirmed from live site inspection:

STEP 1: Recorder detail API → get names[] array
  GET https://publicapi.recorder.maricopa.gov/documents/{id}
  Returns: names[], restricted flag

STEP 2: Assessor NAME search → get APN + property address
  GET https://mcassessor.maricopa.gov/mcs/?q=LASTNAME+FIRSTNAME
  Rendered table row: [APN | Owner | "6265 E ADOBE RD, MESA, 85205" | ...]
  Requires Playwright (JS renders the table after page load)

STEP 3: Assessor APN detail page → get mailing address
  GET https://mcassessor.maricopa.gov/mcs/?q={apn_digits_only}
  Rendered text contains:
    "PROPERTY INFORMATION\n6265 E ADOBE RD MESA, AZ 85205"
    "Mailing Address\n6265 E ADOBE RD, MESA, AZ 85205"

All three steps use one shared Playwright browser for efficiency.
Expected coverage: ~95-99% (vs previous 5%).
"""

import asyncio
import logging
import re
import time
from typing import Optional

import requests

log = logging.getLogger("enricher")

ASSESSOR_BASE = "https://mcassessor.maricopa.gov"
RECORDER_API  = "https://publicapi.recorder.maricopa.gov"
PORTAL_BASE   = "https://recorder.maricopa.gov"

REQUEST_DELAY = 0.5   # seconds between assessor requests (be polite)
TIMEOUT       = 25
MAX_RETRIES   = 2

SUFFIXES = {"JR", "SR", "II", "III", "IV", "TRUST", "LLC", "CORP", "INC", "LP", "LLP", "ET", "AL"}

RECORDER_SESSION = requests.Session()
RECORDER_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept":     "application/json",
    "Referer":    f"{PORTAL_BASE}/recording/document-search-results.html",
    "Origin":     PORTAL_BASE,
})


# ── Public entry point ─────────────────────────────────────────────────────────
def enrich_records(records: list[dict]) -> list[dict]:
    return asyncio.run(_enrich_all(records))


async def _enrich_all(records: list[dict]) -> list[dict]:
    from playwright.async_api import async_playwright

    enriched = []
    total = len(records)
    log.info(f"Enriching {total} records...")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
        )
        page = await ctx.new_page()
        page.set_default_timeout(30_000)

        # Warm up the assessor session (establishes cookies)
        try:
            await page.goto(f"{ASSESSOR_BASE}/mcs/?q=maricopa", wait_until="domcontentloaded", timeout=30_000)
            await asyncio.sleep(2)
            log.info("Assessor session warmed up")
        except Exception as e:
            log.warning(f"Assessor warmup failed (non-fatal): {e}")

        for i, rec in enumerate(records):
            try:
                rec = await _enrich_one(rec, page)
                if rec.get("prop_address"):
                    log.debug(f"  [{i+1}/{total}] ✓ {rec['doc_num']} → {rec['prop_address']}")
                else:
                    log.debug(f"  [{i+1}/{total}] ✗ {rec['doc_num']} — no address found")
            except Exception as exc:
                log.warning(f"  [{i+1}/{total}] Enrichment error for {rec.get('doc_num')}: {exc}")
            enriched.append(rec)
            await asyncio.sleep(REQUEST_DELAY)

        await browser.close()

    with_addr = sum(1 for r in enriched if r.get("prop_address"))
    log.info(f"Enrichment done: {with_addr}/{total} addresses found ({100*with_addr//max(total,1)}%)")
    return enriched


async def _enrich_one(rec: dict, page) -> dict:
    # ── STEP 1: Recorder API → names ──────────────────────────────────────
    detail = _fetch_recorder_detail(rec.get("doc_num", ""))
    if detail:
        rec = _assign_names(rec, detail.get("names") or [])

    # ── STEP 2: Assessor name search → APN + property address ─────────────
    query = _build_query(rec)
    if query:
        apn, prop_addr = await _assessor_name_search(page, query)
        if prop_addr:
            rec["prop_address"] = prop_addr.get("street")
            rec["prop_city"]    = prop_addr.get("city")
            rec["prop_state"]   = prop_addr.get("state") or "AZ"
            rec["prop_zip"]     = prop_addr.get("zip")
        if apn and not rec.get("parcel"):
            rec["parcel"] = apn

        # ── STEP 3: APN detail page → mailing address ─────────────────────
        target_apn = apn or rec.get("parcel")
        if target_apn:
            mail, prop2 = await _assessor_apn_detail(page, target_apn)
            # Use property address from detail if name search didn't get it
            if prop2 and not rec.get("prop_address"):
                rec["prop_address"] = prop2.get("street")
                rec["prop_city"]    = prop2.get("city")
                rec["prop_state"]   = prop2.get("state") or "AZ"
                rec["prop_zip"]     = prop2.get("zip")
            if mail:
                rec["mail_address"] = mail.get("street")
                rec["mail_city"]    = mail.get("city")
                rec["mail_state"]   = mail.get("state") or "AZ"
                rec["mail_zip"]     = mail.get("zip")

    # Fall back: if prop found but no mail, use prop as mail
    if rec.get("prop_address") and not rec.get("mail_address"):
        rec["mail_address"] = rec["prop_address"]
        rec["mail_city"]    = rec["prop_city"]
        rec["mail_state"]   = rec["prop_state"]
        rec["mail_zip"]     = rec["prop_zip"]

    return rec


# ── STEP 1: Recorder detail API ────────────────────────────────────────────────
def _fetch_recorder_detail(doc_num: str) -> Optional[dict]:
    if not doc_num:
        return None
    url = f"{RECORDER_API}/documents/{doc_num}"
    try:
        resp = RECORDER_SESSION.get(url, timeout=TIMEOUT)
        if resp.ok:
            return resp.json()
    except Exception as exc:
        log.debug(f"Recorder detail failed for {doc_num}: {exc}")
    return None


# ── STEP 2: Assessor name search ───────────────────────────────────────────────
async def _assessor_name_search(page, query: str) -> tuple[Optional[str], Optional[dict]]:
    """
    Returns (apn, address_dict) from the first real property result row.
    Confirmed table columns: APN | Owner | Address | Subdivision | MCR | S/T/R | Type
    Address format: "6265 E ADOBE RD, MESA, 85205"
    """
    url = f"{ASSESSOR_BASE}/mcs/?q={requests.utils.quote(query)}"

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)

            # Wait for JS to populate the results table
            try:
                await page.wait_for_function(
                    """() => {
                        const rows = document.querySelectorAll('table tbody tr td');
                        return rows.length > 0 && rows[0].innerText.trim() !== '';
                    }""",
                    timeout=12_000,
                )
            except Exception:
                pass  # No results is fine

            await asyncio.sleep(0.3)

            result = await page.evaluate("""
                () => {
                    const rows = document.querySelectorAll('table tbody tr');
                    for (const row of rows) {
                        const cells = Array.from(row.querySelectorAll('td'))
                                          .map(td => td.innerText.trim());
                        // Real property row: APN matches XXX-XX-XXX pattern
                        if (cells.length >= 3 && /^\\d{3}-\\d{2}-\\d{3}/.test(cells[0])) {
                            return { apn: cells[0], address: cells[2] || '' };
                        }
                    }
                    return null;
                }
            """)

            if result and result.get("address"):
                apn  = result["apn"].strip()
                addr = _parse_addr(result["address"])
                return apn, addr

            return None, None

        except Exception as exc:
            log.debug(f"Name search attempt {attempt} failed for '{query}': {exc}")
            if attempt < MAX_RETRIES:
                await asyncio.sleep(3 * attempt)

    return None, None


# ── STEP 3: Assessor APN detail ────────────────────────────────────────────────
async def _assessor_apn_detail(page, apn: str) -> tuple[Optional[dict], Optional[dict]]:
    """
    Returns (mailing_address, property_address) from the APN detail page.
    Confirmed text format:
      "PROPERTY INFORMATION\n6265 E ADOBE RD MESA, AZ 85205"
      "Mailing Address\n6265 E ADOBE RD, MESA, AZ 85205"
    """
    apn_digits = re.sub(r"[^0-9]", "", apn)
    if len(apn_digits) < 8:
        return None, None

    url = f"{ASSESSOR_BASE}/mcs/?q={apn_digits}"

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)

            # Wait for parcel detail content to load
            try:
                await page.wait_for_function(
                    "() => document.body.innerText.includes('PROPERTY INFORMATION') || "
                    "      document.body.innerText.includes('Owner')",
                    timeout=12_000,
                )
            except Exception:
                pass

            await asyncio.sleep(0.3)

            text = await page.evaluate("() => document.body.innerText")

            prop_addr = _extract_prop_addr_from_detail(text)
            mail_addr = _extract_mail_addr_from_detail(text)

            return mail_addr, prop_addr

        except Exception as exc:
            log.debug(f"APN detail attempt {attempt} failed for {apn}: {exc}")
            if attempt < MAX_RETRIES:
                await asyncio.sleep(3 * attempt)

    return None, None


# ── Address extraction from detail page text ───────────────────────────────────
def _extract_prop_addr_from_detail(text: str) -> Optional[dict]:
    """
    Extract from: "PROPERTY INFORMATION\n6265 E ADOBE RD MESA, AZ 85205"
    or inline:    "located at 6265 E ADOBE RD MESA, AZ 85205"
    """
    patterns = [
        r"PROPERTY INFORMATION\s*\n([^\n]+)",
        r"located at ([^\n.]+AZ\s+\d{5})",
        r"located at ([^\n.]+\d{5})",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            addr = _parse_addr(m.group(1).strip())
            if addr and addr.get("street"):
                return addr
    return None


def _extract_mail_addr_from_detail(text: str) -> Optional[dict]:
    """
    Extract from: "Mailing Address\n6265 E ADOBE RD, MESA, AZ 85205"
    """
    m = re.search(r"Mailing Address\s*\n([^\n]+)", text)
    if m:
        addr = _parse_addr(m.group(1).strip())
        if addr and addr.get("street"):
            return addr
    return None


# ── Address parser ─────────────────────────────────────────────────────────────
def _parse_addr(raw: str) -> Optional[dict]:
    """
    Handles multiple formats from Assessor:
      "6265 E ADOBE RD, MESA, 85205"
      "6265 E ADOBE RD, MESA, AZ 85205"
      "6265 E ADOBE RD MESA, AZ 85205"
    """
    if not raw or not raw.strip():
        return None
    raw = raw.strip()

    # Format with commas: street, city, [state] zip
    m = re.match(
        r"^(.+?),\s*([A-Za-z\s]+?),\s*(?:([A-Z]{2})\s+)?(\d{5}(?:-\d{4})?)$",
        raw,
    )
    if m:
        return {
            "street": m.group(1).strip().title(),
            "city":   m.group(2).strip().title(),
            "state":  (m.group(3) or "AZ").strip(),
            "zip":    m.group(4).strip(),
        }

    # Format without comma before city: "6265 E ADOBE RD MESA, AZ 85205"
    m2 = re.match(
        r"^(\d+\s+.+?)\s+([A-Z][A-Za-z\s]+?),\s*([A-Z]{2})\s+(\d{5})$",
        raw,
    )
    if m2:
        return {
            "street": m2.group(1).strip().title(),
            "city":   m2.group(2).strip().title(),
            "state":  m2.group(3).strip(),
            "zip":    m2.group(4).strip(),
        }

    # Last resort: grab zip and split on first comma
    zip_m = re.search(r"(\d{5})", raw)
    parts = raw.split(",")
    if zip_m and parts:
        return {
            "street": parts[0].strip().title(),
            "city":   parts[1].strip().title() if len(parts) > 1 else "",
            "state":  "AZ",
            "zip":    zip_m.group(1),
        }

    return None


# ── Name helpers ───────────────────────────────────────────────────────────────
def _assign_names(rec: dict, names: list) -> dict:
    if not names:
        return rec

    if rec.get("lead_key") == "NS":
        # NTS order confirmed: [Trustee, Lender, Owner] — owner is last
        rec["trustee_name"] = names[0] if names else None
        rec["owner"]        = names[-1] if len(names) >= 2 else names[0]
        rec["grantee"]      = names[1] if len(names) >= 2 else ""
    else:
        rec["owner"]   = names[0] if names else ""
        rec["grantee"] = names[1] if len(names) > 1 else ""

    parsed = _parse_name(rec.get("owner", ""))
    rec["first_name"]   = parsed["first"]
    rec["last_name"]    = parsed["last"]
    rec["first_name_2"] = ""
    rec["last_name_2"]  = ""

    # Co-owner: check for slash separator (Assessor uses LASTNAME1/LASTNAME2)
    owner = rec.get("owner", "")
    if "/" in owner:
        parts = owner.split("/", 1)
        p2 = _parse_name(parts[1].strip())
        rec["first_name_2"] = p2["first"]
        rec["last_name_2"]  = p2["last"]
    elif len(names) >= 2 and rec.get("lead_key") != "NS":
        p2 = _parse_name(names[1])
        if p2["last"] and p2["last"].upper() not in SUFFIXES:
            rec["first_name_2"] = p2["first"]
            rec["last_name_2"]  = p2["last"]

    return rec


def _build_query(rec: dict) -> Optional[str]:
    last  = (rec.get("last_name")  or "").strip()
    first = (rec.get("first_name") or "").strip()
    if last and first:
        return f"{last} {first}"
    owner = (rec.get("owner") or "").strip()
    if owner:
        # Remove entity suffixes for better search
        cleaned = re.sub(r"\b(LLC|CORP|INC|TRUST|LP|LLP|ET\s+AL|ETAL)\b", "", owner, flags=re.I)
        cleaned = cleaned.strip(" ,/")
        return cleaned[:60] if cleaned else None
    return None


def _parse_name(raw: str) -> dict:
    tokens = raw.upper().split()
    while tokens and tokens[-1] in SUFFIXES:
        tokens.pop()
    if not tokens:
        return {"first": "", "last": ""}
    if len(tokens) == 1:
        return {"first": "", "last": tokens[0].title()}
    return {"last": tokens[0].title(), "first": " ".join(tokens[1:]).title()}
