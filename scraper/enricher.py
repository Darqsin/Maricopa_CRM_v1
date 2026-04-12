"""
scraper/enricher.py  v11

Fixes:
1. Address parser: city must not contain street types (St, Ave, Dr, etc.)
2. "Is purported to be:" prefix stripped from property address
3. Auction date: handles "JULY 7" / "JULY 7," / handwritten month+day
4. Trustee: "Name and Address of Trustee" label takes absolute priority
   Beneficiary label excluded — lender != trustee
5. 2nd owner: populated from both OCR and recorder API names
6. Loan amount trailing spaces stripped
7. Mailing: only captured when clearly a person's address
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

# Street type suffixes — if these appear in a city field it's wrong
STREET_TYPES = {
    "ST","STREET","AVE","AVENUE","DR","DRIVE","RD","ROAD","LN","LANE",
    "WAY","BLVD","BOULEVARD","CT","COURT","PL","PLACE","LOOP","TRL",
    "TRAIL","CIR","CIRCLE","PKWY","PARKWAY","HWY","HIGHWAY","FWY",
    "FREEWAY","EXPY","EXPRESSWAY","PI","PLACE",
}

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
    "UNION","CREDIT","FEDERAL","NAVY","DESERT","MOUNTAIN","PLANET",
    "CARRINGTON","SENECA","GUILD","QUALITY","IGLOO","SERIES",
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


async def enrich_records(records: list[dict]) -> list[dict]:
    from playwright.async_api import async_playwright

    try:
        import pytesseract
        from PIL import Image
        ocr_ok = True
        log.info("OCR ready")
    except ImportError:
        ocr_ok = False
        log.warning("pytesseract not installed")

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
                detail = _fetch_recorder_detail(rec.get("doc_num", ""))
                if detail:
                    rec = _assign_names_smart(rec, detail.get("names") or [])

                if ocr_ok:
                    rec = _enrich_via_ocr(rec)

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


# ── Field extraction ───────────────────────────────────────────────────────────
def _extract_all_fields(rec: dict, text: str) -> dict:

    # ── TRUSTEE — label-first priority ────────────────────────────────────
    # "Name and Address of Trustee" / "NAME AND ADDRESS OF TRUSTEE:"
    # This is the only reliable trustee label — do NOT use beneficiary
    trustee_name  = None
    trustee_phone = None

    for pat in [
        # Explicit label (both PDF styles) — grab up to 2 lines, join, cut at descriptor
        r"[Nn]ame\s+and\s+[Aa]ddress\s+of\s+(?:original\s+)?[Tt]rustee[^:\n]*[:\n]\s*([^\n]{2,80}(?:\n[A-Za-z][^\n]{0,60})?)",
        # "Substitute Trustee: NAME"
        r"[Ss]ubstitute\s+[Tt]rustee[:\s]+([^\n,]{3,80}?)(?:,|\n)",
        # Style A: "designation of X as Foreclosure Commissioner"
        r"designation\s+of\s+([A-Z][A-Za-z\s,\.]+?)\s+as\s+(?:[Ff]oreclosure\s+)?[Cc]ommissioner",
        # "X as Foreclosure Commissioner"
        r"([A-Z][A-Za-z\s,\.&]+?)\s+as\s+[Ff]oreclosure\s+[Cc]ommissioner",
    ]:
        m = re.search(pat, text, re.I)
        if m:
            candidate = " ".join(m.group(1).split()).strip()
            # Cut at descriptor words after name
            cut = re.search(
                r",?\s+(?:licensed|real\s+estate|broker|dba\s+\w|qualif|\d{3}[\-\.]|an\s+arizona|irvine|glendale|phoenix|scottsdale|tempe|mesa|chandler|gilbert|peoria|surprise|avondale|goodyear|buckeye)",
                candidate, re.I
            )
            if cut:
                candidate = candidate[:cut.start()].strip().rstrip(",")
            # Reject boilerplate
            bad = {"SALE","OBJECTION","BELIEVE","DEFENSE","ACTION","COURT","FILE",
                   "LICENSED","BROKER","QUALIFICATIONS","REGULATION","AGENCY"}
            if set(candidate.upper().split()) & bad:
                continue
            if len(candidate) > 3:
                trustee_name = candidate[:100]
                break

    if trustee_name:
        rec["trustee_name"] = trustee_name

    # Phone — all phones found; prefer last (usually trustee, not court)
    phones = re.findall(r"\(?\d{3}\)?[\s\-\.]\d{3}[\s\-\.]\d{4}", text)
    if phones:
        rec["trustee_phone"] = phones[-1].strip()

    # ── PROPERTY ADDRESS ───────────────────────────────────────────────────
    if not rec.get("prop_address"):
        for pat in [
            # "IDENTIFIABLE LOCATION: 4037 S. 12th Street Phoenix, AZ 85040"
            r"IDENTIFIABLE\s+LOCATION[:\s]+([^\n]+)",
            # "Commonly known as: 123 Main St, City, AZ 85001"
            r"[Cc]ommonly\s+known\s+as[:\s]+([^\n]+)",
            # "The street address ... is purported to be: 3218 N 198TH LN, BUCKEYE, AZ 85326"
            r"(?:[Ss]treet\s+address[^:]*)?(?:[Pp]urported\s+to\s+be[:\s]*|[Pp]urported\s+address[:\s]*)\s*:?\s*([^\n]{10,100})",
            # "street address and other common designation ... is: ADDR"
            r"street\s+address[^.]{0,80}?(?:is|be)[:\s]+([^\n]{10,100})",
            # "Situs:"
            r"[Ss]itus[:\s]+([^\n]+)",
            # Two-line: number+street \n City, AZ ZIP
            r"(\d{2,5}\s+[NSEW]?\.?\s*[\w\s\.#]{4,50}(?:ST|AVE|DR|RD|LN|WAY|BLVD|CT|PL|LOOP|TRL|CIR|PKWY|STREET|AVENUE|ROAD|COURT|LANE|DRIVE)\b[^\n]{0,15})\n\s*([\w\s]+,\s*AZ\s+\d{5})",
        ]:
            m = re.search(pat, text, re.I)
            if m:
                raw = m.group(1).strip()
                if m.lastindex and m.lastindex > 1:
                    raw = raw + ", " + m.group(2).strip()
                # Strip "Is Purported To Be:" prefix if still present
                raw = re.sub(r"^[Ii]s\s+[Pp]urported\s+[Tt]o\s+[Bb]e[:\s]*", "", raw).strip()
                addr = _parse_addr(raw)
                if addr and addr.get("zip"):
                    rec["prop_address"] = addr["street"]
                    rec["prop_city"]    = addr["city"]
                    rec["prop_state"]   = addr["state"] or "AZ"
                    rec["prop_zip"]     = addr["zip"]
                    break

    # ── MAILING ADDRESS ────────────────────────────────────────────────────
    if not rec.get("mail_address"):
        m = re.search(
            r"(?:[Ww]hen\s+[Rr]ecorded\s+[Mm]ail\s+[Tt]o|WHEN\s+RECORDED\s+MAIL\s+TO)[:\s]*\n((?:[^\n]+\n){1,6})",
            text
        )
        if m:
            block = m.group(1)
            lines = [l.strip() for l in block.strip().splitlines() if l.strip()]
            first_line = lines[0] if lines else ""
            # Only capture if first line is a person (not company) and not the trustee
            if (not _is_company(first_line) and
                    not (trustee_name and first_line.upper() in trustee_name.upper())):
                for line in lines[1:]:
                    addr = _parse_addr(line)
                    if addr and addr.get("zip"):
                        rec["mail_address"] = addr["street"]
                        rec["mail_city"]    = addr["city"]
                        rec["mail_state"]   = addr["state"] or "AZ"
                        rec["mail_zip"]     = addr["zip"]
                        break

    # ── TRUSTOR / OWNER ────────────────────────────────────────────────────
    if not rec.get("last_name"):
        for pat in [
            # Style B: "NAME AND ADDRESS OF TRUSTOR: NAME, address"
            r"[Nn]ame\s+and\s+[Aa]ddress\s+of\s+(?:original\s+)?[Tt]rustor[:\s]+([^\n]{3,150})",
            # Style A: "executed by NAME as trustor"
            r"executed\s+by\s+([A-Z][a-zA-Z\s\.,]+?)\s*,?\s*(?:[Aa]\s+[Ss]ingle|[Aa]\s+[Mm]arried|[Hh]usband|[Ww]ife|as\s+[Tt]rustor|[Tt]rustor\b)",
            r"([A-Z][a-zA-Z\s\.,]{3,60}?)\s+as\s+[Tt]rustor",
            r"[Tt]rustor[:\s]+([A-Z][A-Za-z\s,\.]{3,80}?)(?:\n|,\s*[Aa]\s+[Ss]ingle)",
            r"[Gg]rantor[:\s]+([A-Z][A-Za-z\s,\.]{3,80}?)(?:\n|,)",
            r"[Bb]orrower[:\s]+([A-Z][A-Za-z\s,\.]{3,80}?)(?:\n|,)",
        ]:
            m = re.search(pat, text, re.I)
            if m:
                raw = " ".join(m.group(1).split()).strip().rstrip(",")
                # For "NAME AND ADDRESS OF TRUSTOR:" — split name from embedded address
                addr_start = re.search(r",\s*\d{2,5}\s+[NSEW\d]", raw)
                name_part  = raw[:addr_start.start()].strip().rstrip(",") if addr_start else raw.split(",")[0].strip()

                rec["owner"] = name_part
                is_co = _is_company(name_part)

                if is_co:
                    display = re.sub(
                        r",?\s*(?:AN?\s+ARIZONA|A\s+\w+\s+)?(?:LIMITED\s+LIABILITY\s+COMPANY|LLC|L\.L\.C\.|CORPORATION|CORP|INC\.?)$",
                        "", name_part, flags=re.I
                    ).strip().rstrip(",")
                    rec["first_name"]   = ""
                    rec["last_name"]    = display.title()
                    rec["first_name_2"] = ""
                    rec["last_name_2"]  = ""
                else:
                    # Split on AND for co-owners
                    rel_pat = r"\s*,?\s*\b(?:husband|wife|married|unmarried|a\s+single|woman|man|trustor|grantor|an?)\b.*$"
                    # Strip "HUSBAND AND"/"WIFE AND" relationship connectors before splitting
                    name_clean = re.sub(r",?\s+(?:HUSBAND|WIFE)\s+AND\s+", " AND ", name_part, flags=re.I)
                    parts   = re.split(r"\s+AND\s+", name_clean, maxsplit=1, flags=re.I)

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
                break

    # ── APN / PARCEL ───────────────────────────────────────────────────────
    if not rec.get("parcel"):
        for pat in [
            r"TAX\s+PARCEL\s+NUMBER[:\s]+([\d\-]+)",
            r"APN[:\s]*([\d]{3}[\-\s][\d]{2}[\-\s][\d]{3}[\w\s]*)",
            r"[Pp]arcel\s+(?:[Nn]o\.?|[Nn]umber|#)[:\s]*([\d]{3}[\-\s][\d]{2}[\-\s][\d]{3})",
            r"\b(\d{3}-\d{2}-\d{3}[A-Z]?)\b",
        ]:
            m = re.search(pat, text, re.I)
            if m:
                rec["parcel"] = m.group(1).strip().split()[0].replace(" ", "-")
                break

    # ── AUCTION DATE ───────────────────────────────────────────────────────
    if not rec.get("auction_date"):
        for pat in [
            # "July 15, 2026 at 10:00 AM"
            r"(\w+\s+\d{1,2},?\s+\d{4})\s+at\s+\d{1,2}:\d{2}\s*[APMapm]{2}",
            # "on July 15, 2026 at 10:00"
            r"on\s+(\w+\s+\d{1,2},?\s+\d{4})\s+at\s+\d",
            # "5/14/2026 at 12:00 PM"
            r"(\d{1,2}/\d{1,2}/\d{4})\s+at\s+\d{1,2}:\d{2}\s*[APMapm]{2}",
            r"on\s+(\d{1,2}/\d{1,2}/\d{4})\s+at\s+\d",
            # "JULY 15, 2026" or "JULY 15 2026" — explicit month name
            r"(?:SALE\s+)?(?:DATE[:\s]+)?(?:ON\s+)?((?:JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)\s+\d{1,2},?\s+\d{4})",
            # "JULY ___, 2026" with handwritten number OCR'd as digit or blank
            r"((?:JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)\s+[\d_]+,?\s+\d{4})",
            # "ON JULY 7 ." or "ON JULY 7," — month day only, year from context
            r"ON\s+((?:JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)\s+\d{1,2})[,\.\s].*?(\d{4})",
            r"[Ss]ale\s+[Dd]ate[:\s]+(\w+\s+\d{1,2},?\s+\d{4})",
            r"notice\s+is\s+hereby\s+given\s+that\s+on\s+(\d{1,2}/\d{1,2}/\d{4})",
        ]:
            m = re.search(pat, text, re.I)
            if m:
                if m.lastindex and m.lastindex >= 2:
                    # month+day pattern with separate year group
                    raw = m.group(1).strip() + " " + m.group(2).strip()
                else:
                    raw = m.group(1).strip()
                # Skip if contains blanks/underscores only for the day
                if re.search(r"_{2,}", raw):
                    continue
                d = _norm_date(raw)
                if re.match(r"20\d{2}-\d{2}-\d{2}", d):
                    rec["auction_date"] = d
                    break

    # ── LOAN AMOUNT ────────────────────────────────────────────────────────
    if not rec.get("amount"):
        for pat in [
            r"ORIGINAL\s+PRINCIPAL\s+BALANCE[:\s]+\$?\s*([\d,]+(?:\.\d{2})?)",
            r"[Oo]riginal\s+[Pp]rincipal\s+[Bb]alance[:\s]+\$?\s*([\d,]+(?:\.\d{2})?)",
            r"will\s+bid\s+an\s+estimate\s+of\s+\$([\d,]+\.?\d*)",
            r"reinstat[e\w]*\s+(?:prior[^$]{0,60})?\s*is\s+\$([\d,]+\.?\d*)",
            r"[Oo]riginal\s+(?:principal\s+)?(?:balance|sum|note|indebtedness)[:\s]+\$?\s*([\d,]+(?:\.\d{2})?)",
            r"[Pp]rincipal\s+(?:sum|balance|amount)[:\s]+\$?\s*([\d,]+(?:\.\d{2})?)",
            r"[Uu]npaid\s+[Pp]rincipal\s+[Bb]alance[:\s]+\$?\s*([\d,]+(?:\.\d{2})?)",
            r"[Ll]oan\s+[Aa]mount[:\s]+\$?\s*([\d,]+(?:\.\d{2})?)",
        ]:
            m = re.search(pat, text, re.I)
            if m:
                try:
                    val = float(m.group(1).replace(",", "").strip())
                    if 5_000 < val < 50_000_000:
                        rec["amount"] = val
                        break
                except ValueError:
                    pass

    # ── DEED OF TRUST # ────────────────────────────────────────────────────
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


# ── Address parser ────────────────────────────────────────────────────────────
def _parse_addr(raw: str) -> Optional[dict]:
    if not raw or not raw.strip():
        return None
    raw = " ".join(raw.split()).strip()
    raw = re.sub(r"^[Ii]s\s+[Pp]urported\s+[Tt]o\s+[Bb]e[:\s]*", "", raw).strip()
    raw = re.sub(r"^[Tt]he\s+street\s+address[^:]*is[:\s]*", "", raw, flags=re.I).strip()

    # "street, city, [state] zip"
    m = re.match(r"^(.+?),\s*([A-Za-z][A-Za-z\s]*?),\s*(?:([A-Za-z]{2})\s+)?(\d{5}(?:-\d{4})?)$", raw)
    if m:
        street = m.group(1).strip()
        city   = _fix_city(m.group(2).strip())
        state  = (m.group(3) or "AZ").upper().strip()
        zip_   = m.group(4).strip()
        return {"street": street.title(), "city": city.title(), "state": state, "zip": zip_}

    # No comma before city: "123 Main St City, AZ 85001"
    m2 = re.match(r"^(\d+\s+.+?),?\s*([A-Za-z]{2})\s+(\d{5})$", raw, re.I)
    if m2:
        pre   = m2.group(1).strip()
        state = m2.group(2).upper()
        zip_  = m2.group(3)
        street, city = _split_street_city(pre)
        if city:
            return {"street": street.title(), "city": city.title(), "state": state, "zip": zip_}
        return {"street": pre.title(), "city": "", "state": state, "zip": zip_}

    # Fallback
    zip_m = re.search(r"(\d{5})", raw)
    if zip_m:
        pre = raw[:zip_m.start()].rstrip(",").strip()
        state_m = re.search(r",?\s*([A-Za-z]{2})\s*$", pre)
        state = state_m.group(1).upper() if state_m else "AZ"
        pre = pre[:state_m.start()].strip() if state_m else pre
        parts = pre.split(",")
        if len(parts) >= 2:
            street = parts[0].strip()
            city   = _fix_city(parts[-1].strip())
            return {"street": street.title(), "city": city.title(), "state": state, "zip": zip_m.group(1)}
    return None


def _split_street_city(text: str) -> tuple:
    """Split 'N 198TH LN BUCKEYE' → ('N 198TH LN', 'BUCKEYE')"""
    tokens = text.upper().split()
    last_st = -1
    for i, tok in enumerate(tokens):
        if tok.rstrip(".") in STREET_TYPES:
            last_st = i
    if last_st >= 0 and last_st < len(tokens) - 1:
        return " ".join(tokens[:last_st+1]), " ".join(tokens[last_st+1:])
    return text, ""


def _fix_city(city: str) -> str:
    """If city starts with a street type, extract real city after it."""
    tokens = city.upper().split()
    for i, tok in enumerate(tokens):
        if tok.rstrip(".") in STREET_TYPES and i < len(tokens) - 1:
            return " ".join(tokens[i+1:]).title()
    return city


# ── Name helpers ───────────────────────────────────────────────────────────────
def _is_company(name: str) -> bool:
    if not name:
        return False
    tokens = set(re.split(r"[\s,\.]+", name.upper()))
    return bool(tokens & COMPANY_WORDS)


def _parse_person_name(raw: str) -> dict:
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

    # Mixed case (natural order: First Last) vs ALL CAPS (recorder: LAST FIRST)
    if raw[0].isupper() and not raw.isupper():
        return {"first": " ".join(tokens[:-1]).title(), "last": tokens[-1].title()}
    return {"last": tokens[0].title(), "first": " ".join(tokens[1:]).title()}


def _assign_names_smart(rec: dict, names: list) -> dict:
    """Pre-populate from recorder API names[] (alphabetical). OCR overrides."""
    if not names:
        return rec

    persons   = [n for n in names if not _is_company(n)]
    companies = [n for n in names if _is_company(n)]

    trustee_kw = {"TRUSTEE","RECON","FINANCIAL","MORTGAGE","TITLE","MTC",
                  "COMMISSIONER","OFFICES","LAW","CORPS","CORP"}
    trustee = next(
        (c for c in companies if set(c.upper().split()) & trustee_kw),
        companies[0] if companies else None
    )
    if trustee and not rec.get("trustee_name"):
        rec["trustee_name"] = trustee

    if persons:
        p1_raw    = persons[0]
        rel_pat   = r"\s*,?\s*\b(?:HUSBAND|WIFE|MARRIED|UNMARRIED|A\s+SINGLE|WOMAN|MAN)\b.*$"
        and_parts = re.split(r"\s+AND\s+", p1_raw, maxsplit=1, flags=re.I)

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


def _extract_from_text(pattern: str, text: str) -> Optional[dict]:
    m = re.search(pattern, text)
    return _parse_addr(m.group(1).strip()) if m else None


def _norm_date(raw: str) -> str:
    from datetime import datetime
    raw = raw.strip().rstrip(".")
    for fmt in ("%m/%d/%Y", "%B %d, %Y", "%b %d, %Y", "%B %d %Y",
                "%B %d, %Y", "%b %d %Y", "%m-%d-%Y", "%B %d,  %Y"):
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
