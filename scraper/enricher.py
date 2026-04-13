"""
scraper/enricher.py  v12

Key fixes:
1. COURTHOUSE ADDRESS blocked: "201 W Jefferson" / "201 West Jefferson" never used as property
2. PURPORTED STREET ADDRESS: keyword added (high priority for property)
3. "Phoenix, Arizona" / "Phoenix, Arizo" OCR artifacts fixed in address parser
4. Auction date: require year >= current year (no old dates from deed/trust references)
5. Trustee: deduplicate "Prime Recon LLC Prime Recon LLC"
6. 2nd owner: populated from BOTH OCR "Name and Address of Trustor" AND recorder API
7. Mailing: "When recorded mail to:" recipient must be a person, not company
8. OCR artifacts like "17 3707..." cleaned from street numbers
"""

import asyncio
import io
import logging
import re
import time
from datetime import datetime
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

# Courthouse / court building addresses — never use as property address
COURTHOUSE_PATTERNS = [
    r"201\s+W(?:est)?\.?\s+Jefferson",
    r"Superior\s+Court\s+Building",
    r"Maricopa\s+County\s+Courthouse",
    r"Superior\s+Building",
    r"Maricopa\s+County\s+Court",
]
COURTHOUSE_RE = re.compile("|".join(COURTHOUSE_PATTERNS), re.I)

# US state abbreviations (2-letter) — for state field validation
US_STATES = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN",
    "IA","KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV",
    "NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN",
    "TX","UT","VT","VA","WA","WV","WI","WY","DC",
}

STREET_TYPES = {
    "ST","STREET","AVE","AVENUE","DR","DRIVE","RD","ROAD","LN","LANE",
    "WAY","BLVD","BOULEVARD","CT","COURT","PL","PLACE","LOOP","TRL",
    "TRAIL","CIR","CIRCLE","PKWY","PARKWAY","HWY","FREEWAY","EXPY","PI",
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
    "UNION","CREDIT","NAVY","DESERT","MOUNTAIN","PLANET","CARRINGTON",
    "SENECA","GUILD","QUALITY","IGLOO","SERIES","PIONEER","CITIZENS",
    "NOVA","CLEARWATER","CLEAR","PRIME","STATEWIDE","FORECLOSURE",
}

CURRENT_YEAR = datetime.utcnow().year

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Referer":    f"{PORTAL_BASE}/recording/document-preview.html",
    "Origin":     PORTAL_BASE,
    "Accept":     "image/png,application/json,*/*",
})

RECORDER_SESSION = requests.Session()
RECORDER_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0",
    "Referer":    f"{PORTAL_BASE}/recording/document-search-results.html",
    "Origin":     PORTAL_BASE,
    "Accept":     "application/json",
})


async def enrich_records(records: list[dict]) -> list[dict]:
    from playwright.async_api import async_playwright

    try:
        import pytesseract
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
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
        )
        assessor_page = await ctx.new_page()
        try:
            await assessor_page.goto(f"{ASSESSOR_BASE}/", wait_until="domcontentloaded", timeout=20_000)
            await asyncio.sleep(1)
        except Exception:
            pass

        for i, rec in enumerate(records):
            try:
                # OCR first — reads directly from document, knows section layout
                if ocr_ok:
                    rec = _enrich_via_ocr(rec)

                # Recorder API fills only fields OCR couldn't populate
                detail = _fetch_recorder_detail(rec.get("doc_num", ""))
                if detail:
                    rec = _assign_names_smart(rec, detail.get("names") or [])

                if not rec.get("prop_address"):
                    rec = await _enrich_via_assessor(rec, assessor_page)

                log.debug(f"  [{i+1}/{total}] {'✓' if rec.get('prop_address') else '✗'} "
                          f"{rec.get('doc_num')} | {rec.get('last_name','')} {rec.get('first_name','')} "
                          f"| 2nd: {rec.get('last_name_2','')} {rec.get('first_name_2','')}"
                          f"| {rec.get('prop_address','no address')}")
            except Exception as exc:
                log.warning(f"  [{i+1}/{total}] Error {rec.get('doc_num')}: {exc}")

            enriched.append(rec)
            time.sleep(REQUEST_DELAY)

        await browser.close()

    with_addr = sum(1 for r in enriched if r.get("prop_address"))
    log.info(f"Done: {with_addr}/{total} addresses ({100*with_addr//max(total,1)}%)")
    return enriched


def _enrich_via_ocr(rec: dict) -> dict:
    doc_num  = rec.get("doc_num", "")
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


def _extract_all_fields(rec: dict, text: str) -> dict:

    # ── TRUSTEE ────────────────────────────────────────────────────────────
    # Scope to the labeled "NAME, ADDRESS & TELEPHONE NUMBER OF TRUSTEE" block.
    # Use [^\n]* (zero-or-more) so blank lines between the label and the name
    # don't break the capture. Never pull from the beneficiary block above it.
    trustee_name = None
    trustee_block_m = re.search(
        r"NAME[,\s]+ADDRESS\s*[&]\s*TELEPHONE\s+NUMBER\s+OF\s+TRUSTEE[^\n]*\n((?:[^\n]*\n){1,10})",
        text, re.I
    )
    if trustee_block_m:
        tblock_lines = [l.strip() for l in trustee_block_m.group(1).splitlines() if l.strip()]
        bad = {"SALE","OBJECTION","BELIEVE","DEFENSE","ACTION","COURT","FILE",
               "LICENSED","BROKER","QUALIFICATIONS","REGULATION","AGENCY",
               "FAX","SALES","ONLINE","INFORMATION","AVAILABLE","REQUESTS","WEBSITE"}
        for tl in tblock_lines:
            candidate = " ".join(tl.split()).strip()
            if candidate.startswith("(") or candidate.startswith("["):
                continue  # parenthetical note line
            if set(candidate.upper().split()) & bad:
                continue
            if re.match(r"^\d{3,5}\s+\w", candidate) or re.match(r"^\(?\d{3}\)?", candidate):
                continue  # street address or phone number line
            # Trim ", a member of the State Bar of Arizona" qualifiers
            candidate = re.sub(r",?\s+a\s+member\s+of\s+the\s+State\s+Bar.*$", "", candidate, flags=re.I).strip()
            if len(candidate) > 3:
                words = candidate.split()
                half  = len(words) // 2
                if half > 1 and words[:half] == words[half:]:
                    candidate = " ".join(words[:half])
                trustee_name = candidate[:100]
                break

    # Fallback patterns when the labeled block isn't present
    if not trustee_name:
        for pat in [
            r"[Ss]ubstitute\s+[Tt]rustee[:\s]+([^\n,]{3,80}?)(?:,|\n)",
            r"designation\s+of\s+([A-Z][A-Za-z\s,\.]+?)\s+as\s+(?:[Ff]oreclosure\s+)?[Cc]ommissioner",
            r"([A-Z][A-Za-z\s,\.&]+?)\s+as\s+[Ff]oreclosure\s+[Cc]ommissioner",
        ]:
            m = re.search(pat, text, re.I)
            if m:
                candidate = " ".join(m.group(1).split()).strip()
                cut = re.search(
                    r",?\s+(?:licensed|real\s+estate|broker|dba\s+\w|qualif|\d{3}[\-\.]|an\s+arizona|irvine|glendale|phoenix|scottsdale|tempe|mesa|chandler|gilbert|peoria|surprise|avondale|goodyear|buckeye|ca\s+\d|az\s+\d)",
                    candidate, re.I
                )
                if cut:
                    candidate = candidate[:cut.start()].strip().rstrip(",")
                bad = {"SALE","OBJECTION","BELIEVE","DEFENSE","ACTION","COURT","FILE",
                       "LICENSED","BROKER","QUALIFICATIONS","REGULATION","AGENCY"}
                if set(candidate.upper().split()) & bad:
                    continue
                if len(candidate) > 3:
                    words = candidate.split()
                    half  = len(words) // 2
                    if half > 1 and words[:half] == words[half:]:
                        candidate = " ".join(words[:half])
                    trustee_name = candidate[:100]
                    break

    if trustee_name:
        rec["trustee_name"] = trustee_name

    # Phone: prefer number inside the trustee block to avoid beneficiary phones
    phone_window = text[trustee_block_m.start():trustee_block_m.start()+600] if trustee_block_m else text
    phones = re.findall(r"\(?\d{3}\)?[\s\-\.]\d{3}[\s\-\.]\d{4}", phone_window)
    if phones:
        rec["trustee_phone"] = phones[0].strip()

    # ── PROPERTY ADDRESS ───────────────────────────────────────────────────
    # Capture auction/law-firm addresses so we can reject them below
    _auction_addr_re = re.compile(
        r"(?:public\s+auction|highest\s+bidder|law\s+(?:office|firm)|PLLC|Esq\.?|sold\s+at)"
        r"[^\n]{0,250}?(\d{3,5}\s+[^\n,]{5,60},\s*[A-Za-z\s]+,\s*(?:AZ|Arizona)\s+\d{5})",
        re.I | re.S,
    )
    _auction_addrs = {am.group(1).strip().upper() for am in _auction_addr_re.finditer(text)}

    if not rec.get("prop_address"):
        for pat in [
            r"(?:PURPORTED\s+STREET\s+ADDRESS|[Ss]treet\s+address\s+or\s+identifiable\s+location)[:\s]+([^\n]+)",
            r"IDENTIFIABLE\s+LOCATION[:\s]+([^\n]+)",
            r"[Cc]ommonly\s+known\s+as[:\s]+([^\n]+)",
            r"(?:[Ss]treet\s+address[^:]*)?[Pp]urported\s+to\s+be[:\s]*:?\s*([^\n]{10,100})",
            r"[Ss]treet\s+address[^.]{0,80}?(?:is|be)[:\s]+([^\n]{10,100})",
            r"[Ss]itus[:\s]+([^\n]+)",
            r"(\d{2,5}\s+[NSEW]?\.?\s*[\w\s\.#]{4,50}(?:ST|AVE|DR|RD|LN|WAY|BLVD|CT|PL|LOOP|TRL|CIR|PKWY|STREET|AVENUE|ROAD|COURT|LANE|DRIVE)\b[^\n]{0,15})\n\s*([\w\s]+,\s*AZ\s+\d{5})",
        ]:
            m = re.search(pat, text, re.I)
            if not m:
                continue
            raw = m.group(1).strip()
            if m.lastindex and m.lastindex > 1:
                raw = raw + ", " + m.group(2).strip()
            raw = re.sub(r"^[Ii]s\s+[Pp]urported\s+[Tt]o\s+[Bb]e[:\s]*", "", raw).strip()

            if COURTHOUSE_RE.search(raw):
                continue
            if raw.upper() in _auction_addrs:
                continue
            if trustee_name and re.search(re.escape(trustee_name[:25]), raw, re.I):
                continue

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
            if (not _is_company(first_line) and
                    not (trustee_name and first_line.upper() in trustee_name.upper())):
                for line in lines[1:]:
                    if not re.search(r"^\d", line.strip()):
                        continue
                    addr = _parse_addr(line)
                    if addr and addr.get("zip"):
                        rec["mail_address"] = addr["street"]
                        rec["mail_city"]    = addr["city"]
                        rec["mail_state"]   = addr["state"] or "AZ"
                        rec["mail_zip"]     = addr["zip"]
                        break

    # ── TRUSTOR / OWNER + 2ND OWNER ───────────────────────────────────────
    # Scan the trustor block line-by-line, skipping paren notes, bare city/zip
    # lines, and street address lines to find the actual name. When two people
    # are listed across two lines ("NAME1, ... AND\nNAME2, ..."), join them.
    if not rec.get("last_name"):
        _trustor_name_raw = None

        trustor_block_m = re.search(
            r"[Nn]ame\s+and\s+[Aa]ddress\s+of\s+(?:original\s+)?[Tt]rustor[:\s]*\n((?:[^\n]*\n){1,12})",
            text
        )
        if trustor_block_m:
            tlines = [l.strip() for l in trustor_block_m.group(1).splitlines() if l.strip()]
            for idx, tl in enumerate(tlines):
                if tl.startswith("(") or tl.startswith("["):
                    continue
                if re.match(r"^[A-Za-z][A-Za-z\s]+,\s*[A-Z]{2}\s+\d{5}", tl):
                    continue  # bare city/state/zip
                if re.match(r"^\d{2,5}\s+", tl):
                    continue  # street address
                # Found the name line — check if it ends with AND (multi-line two-person)
                _trustor_name_raw = tl
                if re.search(r"\bAND\s*$", tl, re.I) and idx + 1 < len(tlines):
                    next_line = tlines[idx + 1]
                    if not re.match(r"^\d{2,5}\s+", next_line):
                        _trustor_name_raw = tl.rstrip() + " " + next_line
                break

        # Fallback patterns
        if not _trustor_name_raw:
            for pat in [
                r"executed\s+by\s+([A-Z][a-zA-Z\s\.,]+?)\s*,?\s*(?:[Aa]\s+[Ss]ingle|[Aa]\s+[Mm]arried|[Hh]usband|[Ww]ife|as\s+[Tt]rustor|[Tt]rustor\b)",
                r"([A-Z][a-zA-Z\s\.,]{3,60}?)\s+as\s+[Tt]rustor",
                r"[Tt]rustor[:\s]+([A-Z][A-Za-z\s,\.]{3,80}?)(?:\n|,\s*[Aa]\s+[Ss]ingle)",
                r"[Gg]rantor[:\s]+([A-Z][A-Za-z\s,\.]{3,80}?)(?:\n|,)",
                r"[Bb]orrower[:\s]+([A-Z][A-Za-z\s,\.]{3,80}?)(?:\n|,)",
            ]:
                m = re.search(pat, text, re.I)
                if m:
                    _trustor_name_raw = m.group(1).strip()
                    break

        if _trustor_name_raw:
            raw = " ".join(_trustor_name_raw.split()).strip().rstrip(",")
            addr_start = re.search(r",\s*\d{2,5}\s+[NSEW\d]", raw)
            name_part  = raw[:addr_start.start()].strip().rstrip(",") if addr_start else raw.split(",")[0].strip()

            # Reject if name matches the trustee
            if trustee_name and name_part.upper() in trustee_name.upper():
                name_part = None

            if name_part:
                rec["owner"] = name_part
                is_co = _is_company(name_part)

                if is_co:
                    display = re.sub(
                        r",?\s*(?:AN?\s+ARIZONA|A\s+\w+\s+)?(?:LIMITED\s+LIABILITY\s+COMPANY|LLC|L\.L\.C\.|CORPORATION|CORP|INC\.?|LIMITED\s+PARTNERSHIP|L\.P\.)$",
                        "", name_part, flags=re.I
                    ).strip().rstrip(",")
                    rec["first_name"]   = ""
                    rec["last_name"]    = display.title()
                    rec["first_name_2"] = ""
                    rec["last_name_2"]  = ""
                else:
                    rel_pat = r"\s*,?\s*\b(?:husband|wife|married|unmarried|a\s+single|woman|man|trustor|grantor|an?|not|nor|joint\s+tenants|as\s+his\s+sole|as\s+her\s+sole|sole\s+and|separate\s+property)\b.*$"
                    name_clean = re.sub(r",?\s+(?:HUSBAND|WIFE)\s+AND\s+", " AND ", name_part, flags=re.I)
                    parts = re.split(r"\s+AND\s+", name_clean, maxsplit=1, flags=re.I)

                    p1_raw = re.sub(rel_pat, "", parts[0], flags=re.I).strip().strip(",")
                    p1 = _parse_person_name(p1_raw, from_doc=True)
                    rec["first_name"]   = p1["first"]
                    rec["last_name"]    = p1["last"]
                    rec["first_name_2"] = ""
                    rec["last_name_2"]  = ""

                    if len(parts) > 1:
                        p2_raw = re.sub(rel_pat, "", parts[1], flags=re.I).strip().strip(",")
                        if p2_raw:
                            p2 = _parse_person_name(p2_raw, from_doc=True)
                            if not p2["last"]:
                                p2["last"] = p1["last"]
                            rec["first_name_2"] = p2["first"]
                            rec["last_name_2"]  = p2["last"]

    # ── APN ────────────────────────────────────────────────────────────────
    if not rec.get("parcel"):
        for pat in [
            r"TAX\s+PARCEL\s+NUMBER(?:\(S\))?[:\s]+([\d\-]+)",
            r"APN[:\s]*([\d]{3}[\-\s][\d]{2}[\-\s][\d]{3}[\w\s]*)",
            r"[Pp]arcel\s+(?:[Nn]o\.?|[Nn]umber|#)[:\s]*([\d]{3}[\-\s][\d]{2}[\-\s][\d]{3})",
            r"\b(\d{3}-\d{2}-\d{3}[A-Z]?)\b",
        ]:
            m = re.search(pat, text, re.I)
            if m:
                rec["parcel"] = m.group(1).strip().split()[0].replace(" ", "-")
                break

    # ── AUCTION DATE — require year >= current year ────────────────────────
    if not rec.get("auction_date"):
        for pat in [
            r"(\w+\s+\d{1,2},?\s+\d{4})\s+at\s+\d{1,2}:\d{2}\s*[APMapm]{2}",
            r"on\s+(\w+\s+\d{1,2},?\s+\d{4})\s+at\s+\d",
            r"(\d{1,2}/\d{1,2}/\d{4})\s+at\s+\d{1,2}:\d{2}\s*[APMapm]{2}",
            r"on\s+(\d{1,2}/\d{1,2}/\d{4})\s+at\s+\d",
            r"(?:SALE\s+)?(?:DATE[:\s]+)?(?:ON\s+)?((?:JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)\s+\d{1,2},?\s+\d{4})",
            r"((?:JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)\s+[\d_]+,?\s+\d{4})",
            r"ON\s+((?:JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)\s+\d{1,2})[,\.\s].*?(\d{4})",
            r"[Ss]ale\s+[Dd]ate[:\s]+(\w+\s+\d{1,2},?\s+\d{4})",
            r"notice\s+is\s+hereby\s+given\s+that\s+on\s+(\d{1,2}/\d{1,2}/\d{4})",
        ]:
            m = re.search(pat, text, re.I)
            if not m:
                continue
            raw = (m.group(1).strip() + " " + m.group(2).strip()) if m.lastindex and m.lastindex >= 2 else m.group(1).strip()
            if re.search(r"_{2,}", raw):
                continue
            d = _norm_date(raw)
            if re.match(r"20\d{2}-\d{2}-\d{2}", d):
                year = int(d[:4])
                if year >= CURRENT_YEAR:   # only accept current or future dates
                    rec["auction_date"] = d
                    break

    # ── LOAN AMOUNT ────────────────────────────────────────────────────────
    existing_amount = rec.get("amount")
    try:
        _has_amount = existing_amount is not None and float(str(existing_amount).replace(",","").replace("$","").strip()) > 0
    except (ValueError, TypeError):
        _has_amount = False
    if not _has_amount:
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


# ── Address parser ─────────────────────────────────────────────────────────────
def _parse_addr(raw: str) -> Optional[dict]:
    if not raw or not raw.strip():
        return None
    raw = " ".join(raw.split()).strip()
    raw = re.sub(r"^[Ii]s\s+[Pp]urported\s+[Tt]o\s+[Bb]e[:\s]*", "", raw).strip()
    raw = re.sub(r"^[Tt]he\s+street\s+address[^:]*is[:\s]*", "", raw, flags=re.I).strip()
    # Remove OCR page number artifacts like "17 " at start
    raw = re.sub(r"^\d{1,3}\s+(?=\d{2,5}\s+[NSEW])", "", raw).strip()

    # Skip courthouse addresses
    if COURTHOUSE_RE.search(raw):
        return None

    # "street, city, [state] zip"
    m = re.match(r"^(.+?),\s*([A-Za-z][A-Za-z\s]*?),\s*(?:([A-Za-z]{2,})\s+)?(\d{5}(?:-\d{4})?)$", raw)
    if m:
        street = m.group(1).strip()
        city_raw = m.group(2).strip()
        state_raw = (m.group(3) or "AZ").strip()
        zip_  = m.group(4).strip()

        # Fix "Phoenix, Arizona" → state=AZ (state_raw might be "Arizona" not "AZ")
        state = _normalize_state(state_raw)
        city  = _fix_city(city_raw)
        if city and not COURTHOUSE_RE.search(city):
            return {"street": street.title(), "city": city.title(), "state": state, "zip": zip_}

    # No comma before city: "123 Main St City, AZ 85001"
    m2 = re.match(r"^(\d+\s+.+?),?\s*([A-Za-z]{2,})\s+(\d{5})$", raw, re.I)
    if m2:
        pre   = m2.group(1).strip()
        state = _normalize_state(m2.group(2))
        zip_  = m2.group(3)
        street, city = _split_street_city(pre)
        if city and not COURTHOUSE_RE.search(city):
            return {"street": street.title(), "city": city.title(), "state": state, "zip": zip_}
        if pre and not COURTHOUSE_RE.search(pre):
            return {"street": pre.title(), "city": "", "state": state, "zip": zip_}

    # Fallback
    zip_m = re.search(r"(\d{5})", raw)
    if zip_m:
        pre = raw[:zip_m.start()].rstrip(",").strip()
        state_m = re.search(r",?\s*([A-Za-z]{2,})\s*$", pre)
        state = _normalize_state(state_m.group(1)) if state_m else "AZ"
        pre   = pre[:state_m.start()].strip() if state_m else pre
        parts = pre.split(",")
        if len(parts) >= 2:
            street = parts[0].strip()
            city   = _fix_city(parts[-1].strip())
            if city and not COURTHOUSE_RE.search(city):
                return {"street": street.title(), "city": city.title(), "state": state, "zip": zip_m.group(1)}
    return None


def _normalize_state(raw: str) -> str:
    """Convert 'Arizona', 'Arizo', 'Az' etc → 'AZ'"""
    raw = raw.strip().upper()
    if raw in US_STATES:
        return raw
    # Full state name mapping (common ones)
    names = {
        "ARIZONA":"AZ","CALIFORNIA":"CA","TEXAS":"TX","NEVADA":"NV",
        "UTAH":"UT","COLORADO":"CO","NEW MEXICO":"NM","OKLAHOMA":"OK",
        "KANSAS":"KS","VIRGINIA":"VA","KENTUCKY":"KY",
    }
    if raw in names:
        return names[raw]
    # Partial match — try first 2 chars  
    # Special case: "ARIZO" starts with "AR" (Arkansas) but is Arizona
    if raw.startswith("ARIZO"):
        return "AZ"
    for abbr in US_STATES:
        if raw == abbr[:len(raw)] and len(raw) >= 3:
            return abbr
    return "AZ"  # default


def _split_street_city(text: str) -> tuple:
    tokens = text.upper().split()
    last_st = -1
    for i, tok in enumerate(tokens):
        if tok.rstrip(".").rstrip(",") in STREET_TYPES:
            last_st = i
    if last_st >= 0 and last_st < len(tokens) - 1:
        return " ".join(tokens[:last_st+1]), " ".join(tokens[last_st+1:])
    return text, ""


def _fix_city(city: str) -> str:
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


def _parse_person_name(raw: str, from_doc: bool = False) -> dict:
    """
    Parse a person name.
    from_doc=True  → from OCR/document text: natural FIRST [MIDDLE] LAST order
    from_doc=False → from recorder API: recorder LAST FIRST [MIDDLE] order
    """
    raw = re.sub(
        r"\s*,?\s*\b(?:husband|wife|married|unmarried|a\s+single|woman|man|trustor|grantor)\b.*$",
        "", raw, flags=re.I
    ).strip().strip(",").strip()

    if not raw:
        return {"first": "", "last": ""}
    tokens = raw.split()
    if not tokens:
        return {"first": "", "last": ""}
    if len(tokens) == 1:
        return {"first": "", "last": tokens[0].title()}

    # Mixed case always means natural order (First Last)
    if raw[0].isupper() and not raw.isupper():
        return {"first": " ".join(tokens[:-1]).title(), "last": tokens[-1].title()}

    # ALL CAPS — use source hint
    if from_doc:
        # Document/OCR text uses natural order: FIRST [MIDDLE] LAST
        return {"first": " ".join(tokens[:-1]).title(), "last": tokens[-1].title()}
    else:
        # Recorder API uses recorder order: LAST FIRST [MIDDLE]
        return {"last": tokens[0].title(), "first": " ".join(tokens[1:]).title()}

def _assign_names_smart(rec: dict, names: list) -> dict:
    if not names:
        return rec

    # Strip OCR page-separator artifacts and other junk from name list
    names = [n for n in names if not re.match(r"^-{2,}|^Page\s+\d+", n.strip(), re.I)]
    if not names:
        return rec

    persons   = [n for n in names if not _is_company(n)]
    companies = [n for n in names if _is_company(n)]

    trustee_kw = {"TRUSTEE","RECON","FINANCIAL","MORTGAGE","TITLE","MTC",
                  "COMMISSIONER","OFFICES","LAW","CORPS","CORP","PRIME",
                  "QUALITY","PIONEER","CLEAR","STATEWIDE","PLLC","ESQ"}
    trustee = next(
        (c for c in companies if set(c.upper().split()) & trustee_kw),
        companies[0] if companies else None
    )
    if trustee and not rec.get("trustee_name"):
        words = trustee.split()
        half  = len(words) // 2
        if half > 1 and words[:half] == words[half:]:
            trustee = " ".join(words[:half])
        rec["trustee_name"] = trustee

    # Only fill name fields OCR left blank
    if not rec.get("last_name"):
        trustee_str = (rec.get("trustee_name") or "").upper()
        # Filter any person whose name is contained in the trustee string
        safe_persons = [p for p in persons if p.upper() not in trustee_str] or persons

        if safe_persons:
            p1_raw    = safe_persons[0]
            rel_pat   = r"\s*,?\s*\b(?:HUSBAND|WIFE|MARRIED|UNMARRIED|A\s+SINGLE|WOMAN|MAN)\b.*$"
            name_clean = re.sub(r",?\s+(?:HUSBAND|WIFE)\s+AND\s+", " AND ", p1_raw, flags=re.I)
            and_parts  = re.split(r"\s+AND\s+", name_clean, maxsplit=1, flags=re.I)

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
            elif len(safe_persons) > 1:
                p2 = _parse_person_name(re.sub(rel_pat, "", safe_persons[1], flags=re.I).strip())
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
    raw = raw.strip().rstrip(".")
    for fmt in ("%m/%d/%Y", "%B %d, %Y", "%b %d, %Y", "%B %d %Y",
                "%b %d %Y", "%m-%d-%Y", "%B %d,  %Y"):
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
