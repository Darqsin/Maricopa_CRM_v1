"""
scraper/enricher.py  v9 — NS-ONLY, keyword-driven OCR

Handles multiple NTS document styles by searching for keywords
regardless of position in document. Confirmed working patterns
derived from live OCR testing.

Fields extracted via keyword search:
  TRUSTOR:     "executed by [NAME] as trustor" / "as trustor" / "Trustor:"
  PROPERTY:    "Commonly known as:" / "Situs:" / "located at"
  MAILING:     "When Recorded Mail To" block
  APN:         "APN XXXX" / "Parcel No."
  SALE DATE:   "MM/DD/YYYY at HH:MM AM/PM" / "Sale Date:"
  LOAN:        secretary estimate / reinstatement amount / original balance
  TRUSTEE:     "[Name] as Foreclosure Commissioner" / "Trustee:" / "Substitute Trustee:"
  PHONE:       any phone near trustee block
  DEED #:      "Instrument No. XXXXXXXX"

PNG API (no auth): publicapi.recorder.maricopa.gov/preview/image
  ?recordingNumber={DOC}&suffix=&affidavit=false&pageNumber={N}
"""

import asyncio
import io
import logging
import re
import time
from typing import Optional

import requests

log = logging.getLogger("enricher")

PNG_API       = "https://publicapi.recorder.maricopa.gov/preview/image"
RECORDER_API  = "https://publicapi.recorder.maricopa.gov"
ASSESSOR_BASE = "https://mcassessor.maricopa.gov"
PORTAL_BASE   = "https://recorder.maricopa.gov"

REQUEST_DELAY = 0.3
TIMEOUT       = 20
MAX_RETRIES   = 2

# Words that indicate a name is a company/entity not a person
COMPANY_WORDS = {
    "LLC","INC","CORP","CORPORATION","LLP","LP","TRUST","NA","N.A.",
    "BANK","FINANCIAL","MORTGAGE","LOAN","SERVICING","TITLE","INSURANCE",
    "REALTY","INVESTMENT","CAPITAL","FUND","PARTNERS","ASSOCIATION",
    "FEDERAL","NATIONAL","SECRETARY","DEPARTMENT","HOUSING","URBAN",
    "DEVELOPMENT","QUICKEN","ROCKET","FREEDOM","NEWREZ","SHELLPOINT",
    "TRUSTEE","CORPS","COMPANY","CO","SERVICES","SERVICE","GROUP",
    "HOLDINGS","VENTURES","MANAGEMENT","RECON","LAW","LEGAL","ATTORNEYS",
    "ZBS","MTC","FIRST","AMERICAN","WELLS","FARGO","CHASE","PENNYMAC",
    "LAKEVIEW","REGIONS","OFFICES","OFFICE","HUD","COMPU-LINK","COMPU",
}

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


# ── Entry point ────────────────────────────────────────────────────────────────
async def enrich_records(records: list[dict]) -> list[dict]:
    from playwright.async_api import async_playwright

    try:
        import pytesseract
        from PIL import Image
        ocr_ok = True
        log.info("OCR ready (pytesseract)")
    except ImportError:
        ocr_ok = False
        log.warning("pytesseract not installed — Assessor fallback only")

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
        )
        assessor_page = await ctx.new_page()
        try:
            await assessor_page.goto(f"{ASSESSOR_BASE}/", wait_until="domcontentloaded", timeout=20_000)
            await asyncio.sleep(1)
        except Exception:
            pass

        for i, rec in enumerate(records):
            try:
                # Step 1: Names from recorder API (for non-OCR fields)
                detail = _fetch_recorder_detail(rec.get("doc_num", ""))
                if detail:
                    rec = _assign_names_smart(rec, detail.get("names") or [])

                # Step 2: OCR all pages
                if ocr_ok:
                    rec = _enrich_via_ocr(rec)

                # Step 3: Assessor fallback if no prop address
                if not rec.get("prop_address"):
                    rec = await _enrich_via_assessor(rec, assessor_page)

                # Step 4: Only copy prop→mail if BOTH are missing
                # (many NTS mail addresses go to trustee/HUD, not owner)
                if rec.get("prop_address") and not rec.get("mail_address"):
                    # Only use prop as mail if owner is a person (not HUD/company)
                    mail = rec.get("mail_address", "")
                    if not mail and not _is_company(rec.get("last_name", "")):
                        rec["mail_address"] = rec["prop_address"]
                        rec["mail_city"]    = rec["prop_city"]
                        rec["mail_state"]   = rec["prop_state"]
                        rec["mail_zip"]     = rec["prop_zip"]

                log.debug(f"  [{i+1}/{total}] {'✓' if rec.get('prop_address') else '✗'} "
                          f"{rec.get('doc_num')} | {rec.get('last_name','')} {rec.get('first_name','')} "
                          f"| {rec.get('prop_address','no address')}")

            except Exception as exc:
                log.warning(f"  [{i+1}/{total}] Error {rec.get('doc_num')}: {exc}")

            enriched.append(rec)
            time.sleep(REQUEST_DELAY)

        await browser.close()

    with_addr = sum(1 for r in enriched if r.get("prop_address"))
    log.info(f"Done: {with_addr}/{total} addresses ({100*with_addr//max(total,1)}%)")
    return enriched


# ── OCR pipeline ───────────────────────────────────────────────────────────────
def _enrich_via_ocr(rec: dict) -> dict:
    doc_num = rec.get("doc_num", "")
    all_text = ""

    # Download up to 4 pages (most NTS docs are 2-4 pages)
    for page_num in range(1, 5):
        png = _download_png(doc_num, page_num)
        if png:
            text = _ocr_image(png)
            if text:
                all_text += f"\n--- PAGE {page_num} ---\n" + text
            time.sleep(0.15)
        else:
            break  # No more pages

    if not all_text.strip():
        return rec

    return _extract_all_fields(rec, all_text)


def _download_png(doc_num: str, page_num: int) -> Optional[bytes]:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = SESSION.get(PNG_API, params={
                "recordingNumber": doc_num,
                "suffix": "", "affidavit": "false", "pageNumber": page_num,
            }, timeout=TIMEOUT)
            if resp.ok and "image" in resp.headers.get("content-type", ""):
                return resp.content
            return None  # 404 = no more pages
        except Exception as exc:
            log.debug(f"PNG {doc_num} p{page_num}: {exc}")
            time.sleep(2 * attempt)
    return None


def _ocr_image(png_bytes: bytes) -> str:
    import pytesseract
    from PIL import Image, ImageFilter, ImageEnhance
    img = Image.open(io.BytesIO(png_bytes)).convert("L")
    img = ImageEnhance.Contrast(img).enhance(2.0)
    img = img.filter(ImageFilter.SHARPEN)
    return pytesseract.image_to_string(img, config="--psm 6 --oem 3")


# ── Field extraction — keyword-driven ─────────────────────────────────────────
def _extract_all_fields(rec: dict, text: str) -> dict:

    # ── TRUSTOR / OWNER ────────────────────────────────────────────────────
    # Priority: keyword "as trustor" > "Trustor:" > "as grantor" > "as borrower"
    if not rec.get("last_name"):
        trustor_raw = None
        for pat in [
            # "executed by Julie B. Vance, A Single person as trustor"
            r"executed\s+by\s+([A-Z][a-zA-Z\s\.,]+?)\s*,?\s*(?:[Aa]\s+[Ss]ingle|[Aa]\s+[Mm]arried|[Hh]usband|[Ww]ife|as\s+[Tt]rustor|[Tt]rustor\b)",
            # "Julie B. Vance as trustor"
            r"([A-Z][a-zA-Z\s\.,]{3,60}?)\s+as\s+[Tt]rustor",
            # "Trustor: SMITH JOHN"
            r"[Tt]rustor[:\s]+([A-Z][A-Za-z\s,\.]{3,80}?)(?:\n|,\s*[Aa]\s+[Ss]ingle|as\s+[Tt]rustee)",
            # "Grantor: ..."
            r"[Gg]rantor[:\s]+([A-Z][A-Za-z\s,\.]{3,80}?)(?:\n|,)",
            # "Borrower: ..."
            r"[Bb]orrower[:\s]+([A-Z][A-Za-z\s,\.]{3,80}?)(?:\n|,)",
        ]:
            m = re.search(pat, text, re.I)
            if m:
                trustor_raw = m.group(1).strip().rstrip(",").strip()
                # Clean up line breaks from OCR
                trustor_raw = " ".join(trustor_raw.split())
                break

        if trustor_raw:
            rec["owner"] = trustor_raw
            # Split co-owners on AND
            parts = re.split(r"\s+[Aa][Nn][Dd]\s+", trustor_raw, maxsplit=1)
            # Clean relationship words from each part
            rel_pattern = r"\s*,?\s*\b(?:husband|wife|married|unmarried|a\s+single|woman|man|trustor|an?)\b.*$"

            p1_raw = re.sub(rel_pattern, "", parts[0], flags=re.I).strip().strip(",")
            p1 = _parse_person_name(p1_raw)
            rec["first_name"] = p1["first"]
            rec["last_name"]  = p1["last"]

            rec["first_name_2"] = ""
            rec["last_name_2"]  = ""
            if len(parts) > 1:
                p2_raw = re.sub(rel_pattern, "", parts[1], flags=re.I).strip().strip(",")
                if p2_raw:
                    p2 = _parse_person_name(p2_raw)
                    if not p2["last"]:
                        p2["last"] = p1["last"]  # Same last name
                    rec["first_name_2"] = p2["first"]
                    rec["last_name_2"]  = p2["last"]

    # ── PROPERTY ADDRESS ───────────────────────────────────────────────────
    if not rec.get("prop_address"):
        for pat in [
            # "Commonly known as: 123 Main St, Phoenix, AZ 85001"
            r"[Cc]ommonly\s+known\s+as[:\s]+([^\n]+)",
            # "Situs: ..."
            r"[Ss]itus[:\s]+([^\n]+)",
            # "Street Address: ..."
            r"[Ss]treet\s+[Aa]ddress[:\s]+([^\n]+)",
            # "located at 123 Main..."
            r"(?:property\s+)?(?:is\s+)?located\s+at\s+([^\n,]{10,80}(?:AZ|Arizona)[^\n]{0,30})",
            # "situated at ..."
            r"situated\s+at\s+([^\n]+)",
            # Two-line: street on one line, City, AZ ZIP on next
            r"(\d{3,5}\s+[NSEW]?\.?\s*[\w\s\.#]{5,50}(?:ST|AVE|DR|RD|LN|WAY|BLVD|CT|PL|LOOP|TRL|CIR|PKWY|DRIVE|STREET|AVENUE|ROAD|COURT|PLACE|LANE)\b[^\n]{0,20})\n\s*([\w\s]+,\s*AZ\s+\d{5})",
        ]:
            m = re.search(pat, text, re.I)
            if m:
                raw = m.group(1).strip()
                if m.lastindex and m.lastindex > 1:
                    raw = raw + ", " + m.group(2).strip()
                addr = _parse_addr(raw)
                if addr and addr.get("zip"):
                    rec["prop_address"] = addr["street"]
                    rec["prop_city"]    = addr["city"]
                    rec["prop_state"]   = addr["state"] or "AZ"
                    rec["prop_zip"]     = addr["zip"]
                    break

    # ── MAILING ADDRESS (from "When Recorded Mail To" block) ───────────────
    # Only capture if it's an actual person address (not HUD/company)
    if not rec.get("mail_address"):
        m = re.search(
            r"[Ww]hen\s+[Rr]ecorded\s+[Mm]ail\s+[Tt]o\s*\n((?:[^\n]+\n){1,6})",
            text
        )
        if m:
            block = m.group(1)
            lines = [l.strip() for l in block.strip().splitlines() if l.strip()]
            # Check if recipient looks like a person (not a company)
            first_line = lines[0] if lines else ""
            if not _is_company(first_line):
                # Find address line in block
                for line in lines:
                    addr = _parse_addr(line)
                    if addr and addr.get("zip"):
                        rec["mail_address"] = addr["street"]
                        rec["mail_city"]    = addr["city"]
                        rec["mail_state"]   = addr["state"] or "AZ"
                        rec["mail_zip"]     = addr["zip"]
                        break

    # ── APN / PARCEL ───────────────────────────────────────────────────────
    if not rec.get("parcel"):
        for pat in [
            r"APN\s+([\d]{3}[\-\s][\d]{2}[\-\s][\d]{3}[\w]?)",
            r"[Pp]arcel\s+(?:[Nn]o\.?|[Nn]umber|#)[:\s]*([\d]{3}[\-\s][\d]{2}[\-\s][\d]{3})",
            r"\b(\d{3}-\d{2}-\d{3}[A-Z]?)\b",
        ]:
            m = re.search(pat, text)
            if m:
                rec["parcel"] = m.group(1).strip().replace(" ", "-")
                break

    # ── SALE / AUCTION DATE ────────────────────────────────────────────────
    if not rec.get("auction_date"):
        for pat in [
            # "5/14/2026 at 12:00 PM" — most reliable
            r"(\d{1,2}/\d{1,2}/\d{4})\s+at\s+\d{1,2}:\d{2}\s*[APMapm]{2}",
            # "on 5/14/2026 at 12"
            r"on\s+(\d{1,2}/\d{1,2}/\d{4})\s+at\s+\d",
            # "Sale Date: May 14, 2026"
            r"[Ss]ale\s+[Dd]ate[:\s]+(\w+\s+\d{1,2},?\s+\d{4})",
            # "will be sold at public auction ... May 14, 2026"
            r"(?:sold|occur)\s+at\s+public\s+auction[^.]{0,100}(\w+\s+\d{1,2},\s+\d{4})",
            # "notice is hereby given that on MM/DD/YYYY"
            r"notice\s+is\s+hereby\s+given\s+that\s+on\s+(\d{1,2}/\d{1,2}/\d{4})",
        ]:
            m = re.search(pat, text, re.I)
            if m:
                d = _norm_date(m.group(1).strip())
                if re.match(r"20\d{2}-\d{2}-\d{2}", d):
                    rec["auction_date"] = d
                    break

    # ── LOAN / BID AMOUNT ──────────────────────────────────────────────────
    if not rec.get("amount"):
        for pat in [
            # HUD style: "will bid an estimate of $232,834.82"
            r"will\s+bid\s+an\s+estimate\s+of\s+\$([\d,]+\.?\d*)",
            # Standard: "reinstatement ... is $X"
            r"reinstat[e\w]*\s+(?:prior[^$]{0,60})?\s*is\s+\$([\d,]+\.?\d*)",
            # "entire amount delinquent ... is $X"
            r"delinquent[^$]{0,50}is\s+\$([\d,]+\.\d{2})",
            # "Original principal balance" / "Original note" / "principal sum"
            r"[Oo]riginal\s+(?:principal\s+)?(?:balance|sum|note|indebtedness)[:\s]+\$?\s*([\d,]+(?:\.\d{2})?)",
            r"[Pp]rincipal\s+(?:sum|balance|amount)[:\s]+\$?\s*([\d,]+(?:\.\d{2})?)",
            r"[Uu]npaid\s+[Pp]rincipal\s+[Bb]alance[:\s]+\$?\s*([\d,]+(?:\.\d{2})?)",
            r"[Ll]oan\s+[Aa]mount[:\s]+\$?\s*([\d,]+(?:\.\d{2})?)",
            r"[Dd]eed\s+of\s+[Tt]rust[^$]{0,100}\$\s*([\d,]{5,12}(?:\.\d{2})?)",
        ]:
            m = re.search(pat, text, re.I | re.S)
            if m:
                try:
                    val = float(m.group(1).replace(",", "").replace(" ", ""))
                    if 5_000 < val < 50_000_000:
                        rec["amount"] = val
                        break
                except ValueError:
                    pass

    # ── TRUSTEE / FORECLOSURE COMMISSIONER ────────────────────────────────
    if not rec.get("trustee_name"):
        for pat in [
            # "designation of Law Offices of Jason C. Tatman as Foreclosure Commissioner"
            r"designation\s+of\s+([A-Z][A-Za-z\s,\.]+?)\s+as\s+(?:[Ff]oreclosure\s+)?[Cc]ommissioner",
            # "Law Offices of X as Foreclosure Commissioner"  
            r"([A-Z][A-Za-z\s,\.&]+?)\s+as\s+(?:[Ff]oreclosure\s+)?[Cc]ommissioner",
            # "Substitute Trustee: ..."
            r"[Ss]ubstitute\s+[Tt]rustee[:\s]+([^\n]+)",
            # "Trustee: NAME"
            r"[Tt]rustee[:\s]+([A-Z][A-Za-z0-9\s,\.&]{3,80}?)(?:\n|Phone|Tel|\(|\d{3})",
            # "successor trustee is NAME"
            r"[Ss]uccessor\s+[Tt]rustee\s+is\s+([A-Z][A-Za-z\s,\.&]{3,80}?)(?:\n|,)",
        ]:
            m = re.search(pat, text, re.I)
            if m:
                name = m.group(1).strip()
                # Clean up OCR artifacts
                name = re.sub(r"\s+'s\s+designation.*$", "", name, flags=re.I)
                if len(name) > 3:
                    rec["trustee_name"] = name[:100]
                    break

    # ── TRUSTEE PHONE ──────────────────────────────────────────────────────
    if not rec.get("trustee_phone"):
        # Find phone numbers near the trustee name / commissioner block
        phones = re.findall(r"\(?\d{3}\)?[\s\-\.]\d{3}[\s\-\.]\d{4}", text)
        if phones:
            # Prefer the last phone found (usually the trustee's, not the court's)
            rec["trustee_phone"] = phones[-1].strip()

    # ── DEED OF TRUST NUMBER ───────────────────────────────────────────────
    if not rec.get("deed_of_trust"):
        m = re.search(r"[Ii]nstrument\s+[Nn]o\.?\s+([\d]{8,14})", text)
        if m:
            rec["deed_of_trust"] = m.group(1).strip()

    return rec


# ── Name classification ────────────────────────────────────────────────────────
def _is_company(name: str) -> bool:
    if not name:
        return False
    tokens = set(name.upper().split())
    return bool(tokens & COMPANY_WORDS)


def _assign_names_smart(rec: dict, names: list) -> dict:
    """
    Names[] from recorder API are alphabetical.
    Classify as person vs company. Trustor = person, Trustee = company.
    This runs BEFORE OCR — OCR will override with more accurate data.
    """
    if not names:
        return rec

    persons   = [n for n in names if not _is_company(n)]
    companies = [n for n in names if _is_company(n)]

    # Trustee company: prefer ones with trustee/mortgage/financial keywords
    trustee_kw = {"TRUSTEE","RECON","FINANCIAL","MORTGAGE","TITLE","MTC",
                  "FIRST","AMERICAN","COMMISSIONER","OFFICES","LAW"}
    trustee = next(
        (c for c in companies if set(c.upper().split()) & trustee_kw),
        companies[0] if companies else None
    )
    if trustee:
        rec["trustee_name"] = trustee

    # Primary borrower from persons list
    if persons:
        p1_raw = persons[0]
        and_parts = re.split(r"\s+AND\s+", p1_raw, maxsplit=1, flags=re.I)
        rel_pat = r"\s*,?\s*\b(?:HUSBAND|WIFE|MARRIED|UNMARRIED|A\s+SINGLE|WOMAN|MAN)\b.*$"

        p1 = _parse_person_name(re.sub(rel_pat, "", and_parts[0], flags=re.I).strip())
        rec["first_name"]   = p1["first"]
        rec["last_name"]    = p1["last"]
        rec["first_name_2"] = ""
        rec["last_name_2"]  = ""
        rec["owner"]        = p1_raw

        if len(and_parts) > 1:
            p2_raw = re.sub(rel_pat, "", and_parts[1], flags=re.I).strip().strip(",")
            p2 = _parse_person_name(p2_raw)
            if not p2["last"]:
                p2["last"] = p1["last"]
            rec["first_name_2"] = p2["first"]
            rec["last_name_2"]  = p2["last"]
        elif len(persons) > 1:
            p2 = _parse_person_name(re.sub(rel_pat, "", persons[1], flags=re.I).strip())
            rec["first_name_2"] = p2["first"]
            rec["last_name_2"]  = p2["last"]

    lenders = [c for c in companies if c != trustee]
    rec["grantee"] = lenders[0] if lenders else ""

    return rec


def _parse_person_name(raw: str) -> dict:
    """Parse LASTNAME FIRSTNAME [MIDDLE] — recorder convention."""
    raw = re.sub(
        r"\s*,?\s*\b(?:husband|wife|married|unmarried|a\s+single|woman|man|trustor|grantor|an?)\b.*$",
        "", raw, flags=re.I
    ).strip().strip(",").strip()

    if not raw:
        return {"first": "", "last": ""}

    tokens = raw.split()
    if not tokens:
        return {"first": "", "last": ""}
    if len(tokens) == 1:
        return {"first": "", "last": tokens[0].title()}

    # "Julie B. Vance" — if starts with first name (title case, short)
    # vs "VANCE JULIE B" — all-caps recorder format
    if raw[0].isupper() and not raw.isupper():
        # Mixed case — likely "FirstName LastName" order (from document text)
        return {"first": " ".join(tokens[:-1]).title(), "last": tokens[-1].title()}
    else:
        # ALL CAPS recorder format — "LASTNAME FIRSTNAME"
        return {"last": tokens[0].title(), "first": " ".join(tokens[1:]).title()}


# ── Assessor fallback ──────────────────────────────────────────────────────────
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
        if mail and not _is_company(mail.get("street", "")):
            rec["mail_address"] = mail.get("street")
            rec["mail_city"]    = mail.get("city")
            rec["mail_state"]   = mail.get("state") or "AZ"
            rec["mail_zip"]     = mail.get("zip")

    return rec


async def _assessor_name_search(page, query: str) -> tuple:
    url = f"{ASSESSOR_BASE}/mcs/?q={requests.utils.quote(query)}"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            try:
                await page.wait_for_function(
                    "() => { const r=document.querySelectorAll('table tbody tr td'); return r.length>0&&r[0].innerText.trim()!=''; }",
                    timeout=12_000,
                )
            except Exception:
                pass
            await asyncio.sleep(0.3)
            result = await page.evaluate("""
                () => {
                    for (const row of document.querySelectorAll('table tbody tr')) {
                        const cells=Array.from(row.querySelectorAll('td')).map(td=>td.innerText.trim());
                        if (cells.length>=3 && /^\\d{3}-\\d{2}-\\d{3}/.test(cells[0]))
                            return {apn:cells[0], address:cells[2]||''};
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
                "() => document.body.innerText.includes('PROPERTY INFORMATION')||document.body.innerText.includes('Mailing Address')",
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


# ── Recorder API ───────────────────────────────────────────────────────────────
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


# ── Utilities ──────────────────────────────────────────────────────────────────
def _parse_addr(raw: str) -> Optional[dict]:
    if not raw or not raw.strip():
        return None
    raw = " ".join(raw.split()).strip()

    # "123 Main St, Phoenix, AZ 85001" or "123 Main St, Phoenix, 85001"
    m = re.match(r"^(.+?),\s*([A-Za-z\s]+?),\s*(?:([A-Z]{2})\s+)?(\d{5}(?:-\d{4})?)$", raw)
    if m:
        return {"street": m.group(1).strip().title(),
                "city":   m.group(2).strip().title(),
                "state":  (m.group(3) or "AZ").strip(),
                "zip":    m.group(4).strip()}

    # "123 Main St Phoenix, AZ 85001"
    m2 = re.match(r"^(\d+\s+.+?)\s+([A-Z][A-Za-z\s]+?),\s*([A-Z]{2})\s+(\d{5})$", raw)
    if m2:
        return {"street": m2.group(1).strip().title(),
                "city":   m2.group(2).strip().title(),
                "state":  m2.group(3).strip(),
                "zip":    m2.group(4).strip()}

    zip_m = re.search(r"(\d{5})", raw)
    parts = raw.split(",")
    if zip_m and parts:
        return {"street": parts[0].strip().title(),
                "city":   parts[1].strip().title() if len(parts) > 1 else "",
                "state":  "AZ", "zip": zip_m.group(1)}
    return None


def _extract_from_text(pattern: str, text: str) -> Optional[dict]:
    m = re.search(pattern, text)
    return _parse_addr(m.group(1).strip()) if m else None


def _norm_date(raw: str) -> str:
    from datetime import datetime
    raw = raw.strip()
    for fmt in ("%m/%d/%Y", "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw


def _build_query(rec: dict) -> Optional[str]:
    last  = (rec.get("last_name")  or "").strip()
    first = (rec.get("first_name") or "").strip()
    if last and first and not _is_company(last) and not _is_company(first):
        return f"{last} {first}"
    owner = (rec.get("owner") or "").strip()
    if owner and not _is_company(owner):
        cleaned = re.sub(r"\s+[Aa][Nn][Dd]\s+.*$", "", owner).strip()
        return cleaned[:60] if cleaned else None
    return None
