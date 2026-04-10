"""
scraper/enricher.py  v7 — CONFIRMED WORKING

PNG Image API (no login required):
  GET https://publicapi.recorder.maricopa.gov/preview/image
      ?recordingNumber={DOC_NUM}&suffix=&affidavit=false&pageNumber=1
  Headers: Referer: https://recorder.maricopa.gov/recording/document-preview.html
  Returns: image/png  (confirmed 200 for all tested docs)

Pipeline per record:
  1. Download page 1 PNG via requests
  2. OCR with pytesseract → extract property address, mailing address,
     trustee name/phone, auction date, loan amount, owner name
  3. Download page 2 PNG if page 1 missing key fields
  4. Fallback: Assessor name search if OCR yields no address
"""

import asyncio
import base64
import io
import logging
import re
import time
from typing import Optional

import requests

log = logging.getLogger("enricher")

PNG_API     = "https://publicapi.recorder.maricopa.gov/preview/image"
RECORDER_API = "https://publicapi.recorder.maricopa.gov"
ASSESSOR_BASE = "https://mcassessor.maricopa.gov"
PORTAL_BASE   = "https://recorder.maricopa.gov"

REQUEST_DELAY = 0.3
TIMEOUT       = 20
MAX_RETRIES   = 2

SUFFIXES = {"JR","SR","II","III","IV","TRUST","LLC","CORP","INC","LP","LLP","ET","AL"}

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Referer":    f"{PORTAL_BASE}/recording/document-preview.html",
    "Origin":     PORTAL_BASE,
    "Accept":     "image/png,application/json,*/*",
})

RECORDER_SESSION = requests.Session()
RECORDER_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Referer":    f"{PORTAL_BASE}/recording/document-search-results.html",
    "Origin":     PORTAL_BASE,
    "Accept":     "application/json",
})


# ── Entry point — called with await from fetch.py ──────────────────────────────
async def enrich_records(records: list[dict]) -> list[dict]:
    enriched = []
    total = len(records)
    log.info(f"Enriching {total} records via PNG OCR...")

    # Check pytesseract is available
    try:
        import pytesseract
        from PIL import Image
        ocr_available = True
        log.info("OCR engine ready (pytesseract)")
    except ImportError:
        ocr_available = False
        log.warning("pytesseract not available — falling back to Assessor search only")

    # Set up Playwright for Assessor fallback
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        )
        assessor_page = await ctx.new_page()
        # Warm up assessor session
        try:
            await assessor_page.goto(f"{ASSESSOR_BASE}/", wait_until="domcontentloaded", timeout=20_000)
            await asyncio.sleep(1)
        except Exception:
            pass

        for i, rec in enumerate(records):
            try:
                # Step 1: Get names from recorder API
                detail = _fetch_recorder_detail(rec.get("doc_num", ""))
                if detail:
                    rec = _assign_names(rec, detail.get("names") or [])

                # Step 2: Download PNG + OCR
                if ocr_available:
                    rec = _enrich_via_png_ocr(rec)

                # Step 3: Assessor fallback if no address yet
                if not rec.get("prop_address"):
                    rec = await _enrich_via_assessor(rec, assessor_page)

                status = "✓" if rec.get("prop_address") else "✗"
                log.debug(f"  [{i+1}/{total}] {status} {rec.get('doc_num')} {rec.get('prop_address','no address')}")

            except Exception as exc:
                log.warning(f"  [{i+1}/{total}] Error {rec.get('doc_num')}: {exc}")

            enriched.append(rec)
            time.sleep(REQUEST_DELAY)

        await browser.close()

    with_addr = sum(1 for r in enriched if r.get("prop_address"))
    log.info(f"Enrichment done: {with_addr}/{total} addresses ({100*with_addr//max(total,1)}%)")
    return enriched


# ── PNG download + OCR ─────────────────────────────────────────────────────────
def _enrich_via_png_ocr(rec: dict) -> dict:
    doc_num   = rec.get("doc_num", "")
    page_count = 2  # always try at least 2 pages

    all_text = ""
    for page_num in range(1, page_count + 1):
        png_bytes = _download_png(doc_num, page_num)
        if png_bytes:
            text = _ocr_image(png_bytes)
            if text:
                all_text += f"\n--- PAGE {page_num} ---\n" + text
        time.sleep(0.2)

    if not all_text.strip():
        return rec

    log.debug(f"  {doc_num} OCR text snippet: {all_text[:200]}")
    rec = _parse_and_merge(rec, all_text)
    return rec


def _download_png(doc_num: str, page_num: int = 1) -> Optional[bytes]:
    params = {
        "recordingNumber": doc_num,
        "suffix":          "",
        "affidavit":       "false",
        "pageNumber":      page_num,
    }
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = SESSION.get(PNG_API, params=params, timeout=TIMEOUT)
            if resp.ok and resp.headers.get("content-type", "").startswith("image"):
                return resp.content
            log.debug(f"  PNG {doc_num} p{page_num}: HTTP {resp.status_code}")
        except Exception as exc:
            log.debug(f"  PNG download attempt {attempt}: {exc}")
        time.sleep(2 * attempt)
    return None


def _ocr_image(png_bytes: bytes) -> str:
    import pytesseract
    from PIL import Image, ImageFilter, ImageEnhance
    img = Image.open(io.BytesIO(png_bytes)).convert("L")
    # Enhance contrast for better OCR
    img = ImageEnhance.Contrast(img).enhance(2.0)
    img = img.filter(ImageFilter.SHARPEN)
    return pytesseract.image_to_string(img, config="--psm 6")


# ── Parse OCR text → extract all NTS fields ────────────────────────────────────
def _parse_and_merge(rec: dict, text: str) -> dict:
    """Extract every useful field from the full OCR text."""

    # ── Property address ───────────────────────────────────────────────────
    if not rec.get("prop_address"):
        for pat in [
            r"(?:property|premises|trust\s+property)\s+(?:is\s+)?(?:located\s+at|known\s+as|described\s+as)[:\s]+([^\n]{10,80})",
            r"(?:street\s+address|situs\s+address)[:\s]+([^\n]{10,80})",
            # Common NTS format: address line followed by City, AZ XXXXX
            r"(\d{2,5}\s+[NSEW]?\s*\w[\w\s\.#]{5,50}(?:ST|AVE|DR|RD|LN|WAY|BLVD|CT|PL|LOOP|TRL|CIR)\b[^\n]{0,30})\n\s*(\w[\w\s]+,\s*AZ\s+\d{5})",
        ]:
            m = re.search(pat, text, re.I)
            if m:
                raw = m.group(1).strip()
                if len(m.groups()) > 1:
                    raw = raw + ", " + m.group(2).strip()
                addr = _parse_addr(raw)
                if addr and addr.get("zip"):
                    rec["prop_address"] = addr["street"]
                    rec["prop_city"]    = addr["city"]
                    rec["prop_state"]   = addr["state"] or "AZ"
                    rec["prop_zip"]     = addr["zip"]
                    break

    # ── Mailing address (trustor/grantor address block) ────────────────────
    if not rec.get("mail_address"):
        for pat in [
            r"[Ww]hen\s+recorded[,\s]+(?:return\s+to|mail\s+to)[:\s]*\n?((?:[^\n]+\n){1,4})",
            r"[Tt]rustor[:\s]+([A-Z][^\n]{5,60})\n([^\n]{5,80}\d{5})",
        ]:
            m = re.search(pat, text, re.I)
            if m:
                block = m.group(0)
                # Find an address line in the block
                addr_m = re.search(
                    r"(\d{2,5}\s+[^\n,]{5,60},\s*[A-Za-z\s]+,?\s*(?:AZ)?\s*\d{5})", block
                )
                if addr_m:
                    addr = _parse_addr(addr_m.group(1))
                    if addr and addr.get("zip"):
                        rec["mail_address"] = addr["street"]
                        rec["mail_city"]    = addr["city"]
                        rec["mail_state"]   = addr["state"] or "AZ"
                        rec["mail_zip"]     = addr["zip"]
                        break

    # ── Trustee name ───────────────────────────────────────────────────────
    if not rec.get("trustee_name"):
        m = re.search(
            r"(?:Substitute\s+)?[Tt]rustee[:\s]+([A-Z][A-Za-z0-9\s,\.&]{3,80}?)(?:\n|Phone|Tel|\(|\d{3})",
            text
        )
        if m:
            rec["trustee_name"] = m.group(1).strip()[:100]

    # ── Trustee phone ──────────────────────────────────────────────────────
    if not rec.get("trustee_phone"):
        m = re.search(r"\(?\d{3}\)?[\s\-\.]\d{3}[\s\-\.]\d{4}", text)
        if m:
            rec["trustee_phone"] = m.group(0).strip()

    # ── Auction date ───────────────────────────────────────────────────────
    if not rec.get("auction_date"):
        for pat in [
            r"[Ss]ale\s+[Dd]ate[:\s]+(\w+\s+\d{1,2},?\s+\d{4})",
            r"[Aa]uction\s+[Dd]ate[:\s]+(\w+\s+\d{1,2},?\s+\d{4})",
            r"will\s+(?:be\s+sold|occur)[^.]{0,60}?(\w+\s+\d{1,2},\s*\d{4})",
            r"(?:at|on)\s+(\w+\s+\d{1,2},\s+\d{4})(?:\s*at\s+\d)",
        ]:
            m = re.search(pat, text, re.I)
            if m:
                rec["auction_date"] = _norm_date(m.group(1).strip())
                break

    # ── Original loan amount ───────────────────────────────────────────────
    if not rec.get("amount"):
        m = re.search(
            r"[Oo]riginal\s+(?:[Ll]oan|[Nn]ote|[Pp]rincipal)[:\s]+\$?([\d,]+(?:\.\d{2})?)",
            text
        )
        if m:
            try:
                rec["amount"] = float(m.group(1).replace(",", ""))
            except ValueError:
                pass

    # ── Owner/trustor name (if not already set) ────────────────────────────
    if not rec.get("last_name"):
        m = re.search(
            r"[Tt]rustor[:\s]+([A-Z][A-Z\s,/\.]{3,60}?)(?:\n|,\s*a\s|\()",
            text
        )
        if m:
            name_raw = m.group(1).strip().rstrip(",")
            if not rec.get("owner"):
                rec["owner"] = name_raw
            parsed = _parse_name(name_raw)
            rec["first_name"] = parsed["first"]
            rec["last_name"]  = parsed["last"]

    # ── Deed of trust number ───────────────────────────────────────────────
    if not rec.get("parcel"):
        m = re.search(r"(?:APN|[Pp]arcel)[:\s#]+(\d{3}[-\s]\d{2}[-\s]\d{3})", text)
        if m:
            rec["parcel"] = m.group(1).replace(" ", "-")

    return rec


# ── Assessor fallback (confirmed working from live testing) ────────────────────
async def _enrich_via_assessor(rec: dict, page) -> dict:
    query = _build_query(rec)
    if not query:
        return rec

    apn, prop_addr = await _assessor_name_search(page, query)
    if prop_addr:
        rec["prop_address"] = prop_addr.get("street")
        rec["prop_city"]    = prop_addr.get("city")
        rec["prop_state"]   = prop_addr.get("state") or "AZ"
        rec["prop_zip"]     = prop_addr.get("zip")
    if apn and not rec.get("parcel"):
        rec["parcel"] = apn

    target_apn = apn or rec.get("parcel")
    if target_apn:
        mail, prop2 = await _assessor_apn_detail(page, target_apn)
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

    if rec.get("prop_address") and not rec.get("mail_address"):
        rec["mail_address"] = rec["prop_address"]
        rec["mail_city"]    = rec["prop_city"]
        rec["mail_state"]   = rec["prop_state"]
        rec["mail_zip"]     = rec["prop_zip"]

    return rec


async def _assessor_name_search(page, query: str) -> tuple:
    url = f"{ASSESSOR_BASE}/mcs/?q={requests.utils.quote(query)}"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            try:
                await page.wait_for_function(
                    "() => { const r = document.querySelectorAll('table tbody tr td'); return r.length > 0 && r[0].innerText.trim() !== ''; }",
                    timeout=12_000,
                )
            except Exception:
                pass
            await asyncio.sleep(0.3)
            result = await page.evaluate("""
                () => {
                    for (const row of document.querySelectorAll('table tbody tr')) {
                        const cells = Array.from(row.querySelectorAll('td')).map(td => td.innerText.trim());
                        if (cells.length >= 3 && /^\\d{3}-\\d{2}-\\d{3}/.test(cells[0]))
                            return { apn: cells[0], address: cells[2] || '' };
                    }
                    return null;
                }
            """)
            if result and result.get("address"):
                return result["apn"].strip(), _parse_addr(result["address"])
            return None, None
        except Exception as exc:
            log.debug(f"Assessor search attempt {attempt} '{query}': {exc}")
            if attempt < MAX_RETRIES:
                await asyncio.sleep(3 * attempt)
    return None, None


async def _assessor_apn_detail(page, apn: str) -> tuple:
    apn_digits = re.sub(r"[^0-9]", "", apn)
    if len(apn_digits) < 8:
        return None, None
    try:
        await page.goto(f"{ASSESSOR_BASE}/mcs/?q={apn_digits}", wait_until="domcontentloaded", timeout=30_000)
        try:
            await page.wait_for_function(
                "() => document.body.innerText.includes('PROPERTY INFORMATION') || document.body.innerText.includes('Mailing Address')",
                timeout=12_000,
            )
        except Exception:
            pass
        await asyncio.sleep(0.3)
        text = await page.evaluate("() => document.body.innerText")
        prop = _extract_from_text(r"PROPERTY INFORMATION\s*\n([^\n]+)", text)
        mail = _extract_from_text(r"Mailing Address\s*\n([^\n]+)", text)
        return mail, prop
    except Exception as exc:
        log.debug(f"APN detail {apn}: {exc}")
        return None, None


def _extract_from_text(pattern: str, text: str) -> Optional[dict]:
    m = re.search(pattern, text)
    if m:
        return _parse_addr(m.group(1).strip())
    return None


# ── Recorder detail API ────────────────────────────────────────────────────────
def _fetch_recorder_detail(doc_num: str) -> Optional[dict]:
    if not doc_num:
        return None
    try:
        resp = RECORDER_SESSION.get(f"{RECORDER_API}/documents/{doc_num}", timeout=TIMEOUT)
        if resp.ok:
            return resp.json()
    except Exception as exc:
        log.debug(f"Recorder detail {doc_num}: {exc}")
    return None


# ── Shared utilities ───────────────────────────────────────────────────────────
def _parse_addr(raw: str) -> Optional[dict]:
    if not raw or not raw.strip():
        return None
    raw = raw.strip()
    # "123 Main St, Phoenix, AZ 85001" or "123 Main St, Phoenix, 85001"
    m = re.match(r"^(.+?),\s*([A-Za-z\s]+?),\s*(?:([A-Z]{2})\s+)?(\d{5}(?:-\d{4})?)$", raw)
    if m:
        return {"street": m.group(1).strip().title(), "city": m.group(2).strip().title(),
                "state": (m.group(3) or "AZ").strip(), "zip": m.group(4).strip()}
    # "123 Main St Phoenix, AZ 85001"
    m2 = re.match(r"^(\d+\s+.+?)\s+([A-Z][A-Za-z\s]+?),\s*([A-Z]{2})\s+(\d{5})$", raw)
    if m2:
        return {"street": m2.group(1).strip().title(), "city": m2.group(2).strip().title(),
                "state": m2.group(3).strip(), "zip": m2.group(4).strip()}
    zip_m = re.search(r"(\d{5})", raw)
    parts = raw.split(",")
    if zip_m and parts:
        return {"street": parts[0].strip().title(),
                "city": parts[1].strip().title() if len(parts) > 1 else "",
                "state": "AZ", "zip": zip_m.group(1)}
    return None


def _norm_date(raw: str) -> str:
    from datetime import datetime
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(raw.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw.strip()


def _assign_names(rec: dict, names: list) -> dict:
    if not names:
        return rec
    if rec.get("lead_key") == "NS":
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
    owner = rec.get("owner", "")
    if "/" in owner:
        p2 = _parse_name(owner.split("/", 1)[1].strip())
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
        cleaned = re.sub(r"\b(LLC|CORP|INC|TRUST|LP|LLP|ET\s+AL|ETAL)\b", "", owner, flags=re.I)
        cleaned = re.sub(r"[/].*", "", cleaned).strip(" ,")
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
