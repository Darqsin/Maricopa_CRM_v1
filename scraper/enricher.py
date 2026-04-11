"""
scraper/enricher.py  v10

Fixes from document analysis (two NTS document styles confirmed):

STYLE A — HUD/Wells Fargo format (doc 20260210293):
  "executed by Julie B. Vance, A Single person as trustor"
  "Commonly known as: 17801 N 45th Ave, Glendale, AZ 85308"
  "notice is hereby given that on 5/14/2026 at 12:00 PM"

STYLE B — Private lender / Arizona broker format (doc 20260214859):
  "NAME AND ADDRESS OF TRUSTOR: RYMAX DEVELOPMENT, L.L.C., ..."
  "IDENTIFIABLE LOCATION: 4037 S. 12th Street Phoenix, AZ 85040"
  "ORIGINAL PRINCIPAL BALANCE: $480,000.00"
  "TAX PARCEL NUMBER: 113-25-057 and 113-25-069"
  "NAME AND ADDRESS OF TRUSTEE (as of date of recording of sale): Ronald B. Herb..."
  "PUBLIC AUCTION ... AT 11:00 AM ARIZONA TIME ON JULY ___, .2026"

Key fixes:
  - TRUSTEE: search "NAME AND ADDRESS OF TRUSTEE:" first; avoid matching boilerplate
  - PROPERTY: "IDENTIFIABLE LOCATION:" takes priority over mailing block
  - TRUSTOR: "NAME AND ADDRESS OF TRUSTOR:" extracts full label including companies
  - MAILING: Only use if recipient is a person AND different from trustee
  - NAMES: If trustor is a company → leave first/last blank, put company in owner
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

COMPANY_WORDS = {
    "LLC","L.L.C.","INC","CORP","CORPORATION","LLP","LP","TRUST",
    "BANK","FINANCIAL","MORTGAGE","LOAN","SERVICING","TITLE","INSURANCE",
    "REALTY","INVESTMENT","CAPITAL","FUND","PARTNERS","ASSOCIATION",
    "FEDERAL","NATIONAL","SECRETARY","DEPARTMENT","HOUSING","URBAN",
    "DEVELOPMENT","QUICKEN","ROCKET","FREEDOM","NEWREZ","SHELLPOINT",
    "TRUSTEE","CORPS","COMPANY","CO","SERVICES","SERVICE","GROUP",
    "HOLDINGS","VENTURES","MANAGEMENT","RECON","LAW","LEGAL","ATTORNEYS",
    "ZBS","MTC","FIRST","AMERICAN","WELLS","FARGO","CHASE","PENNYMAC",
    "LAKEVIEW","REGIONS","OFFICES","OFFICE","HUD","LIMITED","LIABILITY",
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
        log.info("OCR ready")
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
                # Step 1: Names from recorder API
                detail = _fetch_recorder_detail(rec.get("doc_num", ""))
                if detail:
                    rec = _assign_names_smart(rec, detail.get("names") or [])

                # Step 2: OCR all pages
                if ocr_ok:
                    rec = _enrich_via_ocr(rec)

                # Step 3: Assessor fallback if no property address
                if not rec.get("prop_address"):
                    rec = await _enrich_via_assessor(rec, assessor_page)

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
    for page_num in range(1, 5):
        png = _download_png(doc_num, page_num)
        if not png:
            break
        text = _ocr_image(png)
        if text:
            all_text += f"\n--- PAGE {page_num} ---\n" + text
        time.sleep(0.15)

    if all_text.strip():
        rec = _extract_all_fields(rec, all_text)
    return rec


def _download_png(doc_num: str, page_num: int) -> Optional[bytes]:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = SESSION.get(PNG_API, params={
                "recordingNumber": doc_num, "suffix": "",
                "affidavit": "false", "pageNumber": page_num,
            }, timeout=TIMEOUT)
            if resp.ok and "image" in resp.headers.get("content-type", ""):
                return resp.content
            return None
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


# ── Master field extractor ─────────────────────────────────────────────────────
def _extract_all_fields(rec: dict, text: str) -> dict:

    # ── TRUSTEE ────────────────────────────────────────────────────────────
    # Must find trustee BEFORE property so we can exclude trustee's address
    trustee_name  = None
    trustee_phone = None
    trustee_addr  = None   # used to avoid using as property/mail

    for pat in [
        # Style B: "NAME AND ADDRESS OF TRUSTEE (as of date...): Ronald B. Herb, ..." (may span 2 lines)
        r"NAME\s+AND\s+ADDRESS\s+OF\s+TRUSTEE[^:]*:\s*([^\n]{2,60}\n?[A-Za-z][^\n]{0,60})",
        # Style A: "designation of Law Offices of X as Foreclosure Commissioner"
        r"designation\s+of\s+([A-Z][A-Za-z\s,\.]+?)\s+as\s+(?:[Ff]oreclosure\s+)?[Cc]ommissioner",
        # Generic: "X as Foreclosure Commissioner"
        r"([A-Z][A-Za-z\s,\.&]+?)\s+as\s+[Ff]oreclosure\s+[Cc]ommissioner",
        # "Substitute Trustee: NAME"
        r"[Ss]ubstitute\s+[Tt]rustee[:\s]+([^\n,]{3,80}?)(?:,|\n)",
        # "Trustee: NAME" — but NOT the boilerplate warning sentence
        r"[Tt]rustee[:\s]+([A-Z][A-Za-z0-9\s,\.&]{3,80}?)\n",
    ]:
        m = re.search(pat, text, re.I)
        if m:
            candidate = " ".join(m.group(1).split()).strip()
            # Cut at "licensed", "real estate", phone/address after multi-line join
            cut = re.search(r",?\s+(?:licensed|real\s+estate|broker|qualif|\d{3}[\-\.])", candidate, re.I)
            if cut:
                candidate = candidate[:cut.start()].strip().rstrip(",")
            # Reject boilerplate warning text
            boilerplate_words = {"SALE","OBJECTION","BELIEVE","DEFENSE","ACTION","COURT","FILE"}
            if set(candidate.upper().split()) & boilerplate_words:
                continue
            # Reject qualifications lines
            qual_words = {"LICENSED","BROKER","QUALIFICATIONS","REGULATION","AGENCY"}
            if set(candidate.upper().split()) & qual_words:
                continue
            if len(candidate) > 3:
                trustee_name = candidate[:100]
            break

    if trustee_name:
        rec["trustee_name"] = trustee_name

        # Find trustee address block — to avoid using as property/mail
        m_addr = re.search(
            r"NAME\s+AND\s+ADDRESS\s+OF\s+TRUSTEE[^:]*:[^\n]*\n([^\n]{5,80})\n([^\n]{5,60})",
            text, re.I
        )
        if m_addr:
            trustee_addr = m_addr.group(1).strip() + " " + m_addr.group(2).strip()

    # Trustee phone — look near trustee block
    phones = re.findall(r"\(?\d{3}\)?[\s\-\.]\d{3}[\s\-\.]\d{4}", text)
    if phones:
        rec["trustee_phone"] = phones[-1].strip()

    # ── MAILING ADDRESS ────────────────────────────────────────────────────
    # Only capture if: (a) it's a person name, (b) not the trustee's address
    mail_captured = False
    m = re.search(
        r"[Ww]hen\s+[Rr]ecorded\s+[Mm]ail\s+[Tt]o[:\s]*\n((?:[^\n]+\n){1,6})",
        text
    )
    if m:
        block = m.group(1)
        lines = [l.strip() for l in block.strip().splitlines() if l.strip()]
        first_line = lines[0] if lines else ""

        # Is the mail recipient a person (not a company) AND not the trustee?
        if (not _is_company(first_line) and
                trustee_name and first_line.upper() not in trustee_name.upper() and
                trustee_name.upper() not in first_line.upper()):
            # Find the address line in the block
            for line in lines[1:]:  # skip the name line
                addr = _parse_addr(line)
                if addr and addr.get("zip"):
                    rec["mail_address"] = addr["street"]
                    rec["mail_city"]    = addr["city"]
                    rec["mail_state"]   = addr["state"] or "AZ"
                    rec["mail_zip"]     = addr["zip"]
                    mail_captured = True
                    break

    # ── PROPERTY ADDRESS ───────────────────────────────────────────────────
    if not rec.get("prop_address"):
        for pat in [
            # Style B: "IDENTIFIABLE LOCATION: 4037 S. 12th Street Phoenix, AZ 85040"
            r"IDENTIFIABLE\s+LOCATION[:\s]+([^\n]+)",
            # Style A: "Commonly known as: 123 Main St, City, AZ 85001"
            r"[Cc]ommonly\s+known\s+as[:\s]+([^\n]+)",
            # "Situs:"
            r"[Ss]itus[:\s]+([^\n]+)",
            # "Street Address:"
            r"[Ss]treet\s+[Aa]ddress[:\s]+([^\n]+)",
            # "located at X"
            r"(?:property\s+)?(?:is\s+)?located\s+at\s+([^\n,]{10,100})",
            # "situated at X"
            r"situated\s+at\s+([^\n]+)",
            # Two-line street + City, AZ ZIP
            r"(\d{2,5}\s+[NSEW]?\.?\s*[\w\s\.#]{4,50}(?:ST|AVE|DR|RD|LN|WAY|BLVD|CT|PL|LOOP|TRL|CIR|PKWY|STREET|AVENUE|ROAD|COURT|LANE|DRIVE)\b[^\n]{0,15})\n\s*([\w\s]+,\s*AZ\s+\d{5})",
        ]:
            m = re.search(pat, text, re.I)
            if m:
                raw = m.group(1).strip()
                if m.lastindex and m.lastindex > 1:
                    raw = raw + ", " + m.group(2).strip()

                # Skip if this matches the trustee's address
                if trustee_addr and raw[:20].upper() in trustee_addr.upper():
                    continue

                addr = _parse_addr(raw)
                if addr and addr.get("zip"):
                    rec["prop_address"] = addr["street"]
                    rec["prop_city"]    = addr["city"]
                    rec["prop_state"]   = addr["state"] or "AZ"
                    rec["prop_zip"]     = addr["zip"]
                    break

    # ── TRUSTOR / OWNER NAME ───────────────────────────────────────────────
    if not rec.get("last_name"):
        trustor_raw = None
        trustor_is_company = False

        for pat in [
            # Style B: "NAME AND ADDRESS OF TRUSTOR: RYMAX DEVELOPMENT, L.L.C., ..."
            r"NAME\s+AND\s+ADDRESS\s+OF\s+TRUSTOR[:\s]+([^\n]{3,120})",
            # Style A: "executed by Julie B. Vance, A Single person as trustor"
            r"executed\s+by\s+([A-Z][a-zA-Z\s\.,]+?)\s*,?\s*(?:[Aa]\s+[Ss]ingle|[Aa]\s+[Mm]arried|[Hh]usband|[Ww]ife|as\s+[Tt]rustor|[Tt]rustor\b)",
            # "X as trustor"
            r"([A-Z][a-zA-Z\s\.,]{3,60}?)\s+as\s+[Tt]rustor",
            # "Trustor: NAME"
            r"[Tt]rustor[:\s]+([A-Z][A-Za-z\s,\.]{3,80}?)(?:\n|,\s*[Aa]\s+[Ss]ingle)",
            # "Grantor:"
            r"[Gg]rantor[:\s]+([A-Z][A-Za-z\s,\.]{3,80}?)(?:\n|,)",
            # "Borrower:"
            r"[Bb]orrower[:\s]+([A-Z][A-Za-z\s,\.]{3,80}?)(?:\n|,)",
        ]:
            m = re.search(pat, text, re.I)
            if m:
                raw = " ".join(m.group(1).split()).strip().rstrip(",")
                # For "NAME AND ADDRESS OF TRUSTOR:" — extract just the name part
                # before the address (stop at a number that looks like a street address)
                addr_start = re.search(r",\s*\d{2,5}\s+[NSEW]", raw)
                if addr_start:
                    name_part = raw[:addr_start.start()].strip().rstrip(",")
                else:
                    name_part = raw.split(",")[0].strip()

                trustor_raw = name_part
                trustor_is_company = _is_company(trustor_raw)
                break

        if trustor_raw:
            rec["owner"] = trustor_raw

            if trustor_is_company:
                # Company trustor: put company name in last_name field, blank first_name
                # Strip company suffixes for cleaner display
                display = re.sub(r",?\s*(?:AN?\s+ARIZONA|A\s+\w+\s+)?(?:LIMITED\s+LIABILITY\s+COMPANY|LLC|L\.L\.C\.|CORPORATION|CORP|INC\.?)$",
                                 "", trustor_raw, flags=re.I).strip().rstrip(",")
                rec["first_name"] = ""
                rec["last_name"]  = display.title()
                rec["first_name_2"] = ""
                rec["last_name_2"]  = ""
            else:
                # Person trustor
                rel_pat = r"\s*,?\s*\b(?:husband|wife|married|unmarried|a\s+single|woman|man|trustor|grantor|an?)\b.*$"
                parts = re.split(r"\s+[Aa][Nn][Dd]\s+", trustor_raw, maxsplit=1, flags=re.I)

                p1_raw = re.sub(rel_pat, "", parts[0], flags=re.I).strip().strip(",")
                p1 = _parse_person_name(p1_raw)
                rec["first_name"]   = p1["first"]
                rec["last_name"]    = p1["last"]
                rec["first_name_2"] = ""
                rec["last_name_2"]  = ""

                if len(parts) > 1:
                    p2_raw = re.sub(rel_pat, "", parts[1], flags=re.I).strip().strip(",")
                    if p2_raw:
                        p2 = _parse_person_name(p2_raw)
                        if not p2["last"]:
                            p2["last"] = p1["last"]
                        rec["first_name_2"] = p2["first"]
                        rec["last_name_2"]  = p2["last"]

    # ── APN / PARCEL ───────────────────────────────────────────────────────
    if not rec.get("parcel"):
        for pat in [
            # Style B: "TAX PARCEL NUMBER: 113-25-057 and 113-25-069"
            r"TAX\s+PARCEL\s+NUMBER[:\s]+([\d\-]+)",
            r"APN\s+([\d]{3}[\-\s][\d]{2}[\-\s][\d]{3}[\w]?)",
            r"[Pp]arcel\s+(?:[Nn]o\.?|[Nn]umber|#)[:\s]*([\d]{3}[\-\s][\d]{2}[\-\s][\d]{3})",
            r"\b(\d{3}-\d{2}-\d{3}[A-Z]?)\b",
        ]:
            m = re.search(pat, text, re.I)
            if m:
                rec["parcel"] = m.group(1).strip().replace(" ", "-")
                break

    # ── SALE / AUCTION DATE ────────────────────────────────────────────────
    if not rec.get("auction_date"):
        for pat in [
            # "5/14/2026 at 12:00 PM"
            r"(\d{1,2}/\d{1,2}/\d{4})\s+at\s+\d{1,2}:\d{2}\s*[APMapm]{2}",
            r"on\s+(\d{1,2}/\d{1,2}/\d{4})\s+at\s+\d",
            r"[Ss]ale\s+[Dd]ate[:\s]+(\d{1,2}/\d{1,2}/\d{4})",
            r"[Ss]ale\s+[Dd]ate[:\s]+(\w+\s+\d{1,2},?\s+\d{4})",
            r"notice\s+is\s+hereby\s+given\s+that\s+on\s+(\d{1,2}/\d{1,2}/\d{4})",
            r"(?:sold|occur)\s+at\s+public\s+auction[^.]{0,100}?(\w+\s+\d{1,2},\s+\d{4})",
            # "AT 11:00 AM ARIZONA TIME ON JULY 15, 2026"
            r"(?:TIME\s+)?ON\s+(\w+\s+\d{1,2},?\s+\d{4})\s*[.\n]",
        ]:
            m = re.search(pat, text, re.I)
            if m:
                d = _norm_date(m.group(1).strip())
                if re.match(r"20\d{2}-\d{2}-\d{2}", d):
                    rec["auction_date"] = d
                    break

    # ── LOAN AMOUNT ────────────────────────────────────────────────────────
    if not rec.get("amount"):
        for pat in [
            # Style B: "ORIGINAL PRINCIPAL BALANCE: $480,000.00"
            r"ORIGINAL\s+PRINCIPAL\s+BALANCE[:\s]+\$?\s*([\d,]+(?:\.\d{2})?)",
            # Style A: "will bid an estimate of $232,834.82"
            r"will\s+bid\s+an\s+estimate\s+of\s+\$([\d,]+\.?\d*)",
            # "reinstatement ... is $X"
            r"reinstat[e\w]*\s+(?:prior[^$]{0,60})?\s*is\s+\$([\d,]+\.?\d*)",
            r"[Oo]riginal\s+(?:principal\s+)?(?:balance|sum|note|indebtedness)[:\s]+\$?\s*([\d,]+(?:\.\d{2})?)",
            r"[Pp]rincipal\s+(?:sum|balance|amount)[:\s]+\$?\s*([\d,]+(?:\.\d{2})?)",
            r"[Uu]npaid\s+[Pp]rincipal\s+[Bb]alance[:\s]+\$?\s*([\d,]+(?:\.\d{2})?)",
            r"[Ll]oan\s+[Aa]mount[:\s]+\$?\s*([\d,]+(?:\.\d{2})?)",
        ]:
            m = re.search(pat, text, re.I)
            if m:
                try:
                    val = float(m.group(1).replace(",", "").replace(" ", ""))
                    if 5_000 < val < 50_000_000:
                        rec["amount"] = val
                        break
                except ValueError:
                    pass

    # ── DEED OF TRUST NUMBER ───────────────────────────────────────────────
    if not rec.get("deed_of_trust"):
        for pat in [
            r"[Ii]nstrument\s+[Nn]o\.?\s+([\d]{8,14})",
            r"[Rr]ecorder'?s?\s+[Nn]umber\s+([\d]{8,14})",
            r"[Rr]ecording\s+[Nn]o\.?\s+([\d]{8,14})",
        ]:
            m = re.search(pat, text, re.I)
            if m:
                rec["deed_of_trust"] = m.group(1).strip()
                break

    return rec


# ── Name utilities ─────────────────────────────────────────────────────────────
def _is_company(name: str) -> bool:
    if not name:
        return False
    tokens = set(re.split(r"[\s,\.]+", name.upper()))
    return bool(tokens & COMPANY_WORDS)


def _parse_person_name(raw: str) -> dict:
    """
    Parse name from document text.
    Detects format: "Julie B. Vance" (natural) vs "VANCE JULIE B" (recorder).
    """
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

    # If mixed case (like "Julie B. Vance") → FirstName LastName order
    if raw[0].isupper() and not raw.isupper():
        return {"first": " ".join(tokens[:-1]).title(), "last": tokens[-1].title()}

    # ALL CAPS recorder format → LASTNAME FIRSTNAME
    return {"last": tokens[0].title(), "first": " ".join(tokens[1:]).title()}


def _assign_names_smart(rec: dict, names: list) -> dict:
    """Pre-populate names from recorder API; OCR will override with better data."""
    if not names:
        return rec

    persons   = [n for n in names if not _is_company(n)]
    companies = [n for n in names if _is_company(n)]

    trustee_kw = {"TRUSTEE","RECON","FINANCIAL","MORTGAGE","TITLE","MTC",
                  "COMMISSIONER","OFFICES","LAW","FIRST","AMERICAN"}
    trustee = next(
        (c for c in companies if set(c.upper().split()) & trustee_kw),
        companies[0] if companies else None
    )
    if trustee and not rec.get("trustee_name"):
        rec["trustee_name"] = trustee

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
    elif companies:
        # All companies — company is the trustor
        rec["owner"]      = companies[0]
        rec["first_name"] = ""
        rec["last_name"]  = companies[0].title()

    lenders = [c for c in companies if c != trustee]
    rec["grantee"] = lenders[0] if lenders else ""

    return rec


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

    m = re.match(r"^(.+?),\s*([A-Za-z\s]+?),\s*(?:([A-Z]{2})\s+)?(\d{5}(?:-\d{4})?)$", raw)
    if m:
        return {"street": m.group(1).strip().title(),
                "city":   m.group(2).strip().title(),
                "state":  (m.group(3) or "AZ").strip(),
                "zip":    m.group(4).strip()}

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
