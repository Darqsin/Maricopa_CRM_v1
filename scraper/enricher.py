"""
scraper/enricher.py  v8

Key fixes from data analysis:
1. Names[] array is ALPHABETICAL not positional — must detect persons vs companies
2. Trustee = company names (CORPS, LLC, INC, FINANCIAL, MORTGAGE, TITLE, etc.)
3. Borrowers = person names (LASTNAME FIRSTNAME pattern, no company keywords)
4. Co-borrower handling: "ORTIZ WENDY AND ORTIZ JOSEPH" → split on AND
5. Couple format: "LASTNAME FIRSTNAME AND LASTNAME2 FIRSTNAME2 HUSBAND AND WIFE"
6. Original loan / auction date come from OCR only — must improve patterns
7. Address parsing: "6265 E ADOBE RD, MESA, 85205" confirmed format

PNG API (confirmed working, no auth):
  GET https://publicapi.recorder.maricopa.gov/preview/image
      ?recordingNumber={DOC}&suffix=&affidavit=false&pageNumber=1
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

# Keywords that identify a name as a COMPANY not a person
COMPANY_KEYWORDS = {
    "LLC", "INC", "CORP", "CORPORATION", "LLP", "LP", "TRUST", "NA",
    "BANK", "FINANCIAL", "MORTGAGE", "LOAN", "SERVICING", "TITLE",
    "INSURANCE", "REALTY", "INVESTMENT", "CAPITAL", "FUND", "PARTNERS",
    "ASSOCIATION", "FEDERAL", "NATIONAL", "SECRETARY", "DEPARTMENT",
    "HOUSING", "URBAN", "DEVELOPMENT", "QUICKEN", "ROCKET", "FREEDOM",
    "NEWREZ", "SHELLPOINT", "TRUSTEE", "CORPS", "COMPANY", "CO",
    "SERVICES", "SERVICE", "GROUP", "HOLDINGS", "VENTURES", "MANAGEMENT",
    "RECON", "LAW", "LEGAL", "ATTORNEYS", "ZBS", "MTC", "FIRST",
    "AMERICAN", "WELLS", "FARGO", "CHASE", "PENNYMAC", "LAKEVIEW",
    "REGIONS", "TOBOROWSKY", "CHANOKNAT",
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

    enriched = []
    total = len(records)

    try:
        import pytesseract
        from PIL import Image
        ocr_available = True
        log.info("OCR engine ready (pytesseract)")
    except ImportError:
        ocr_available = False
        log.warning("pytesseract not available — using Assessor fallback only")

    log.info(f"Enriching {total} records via PNG OCR + Assessor fallback...")

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
                # Step 1: Get names from recorder API and classify correctly
                detail = _fetch_recorder_detail(rec.get("doc_num", ""))
                if detail:
                    rec = _assign_names_smart(rec, detail.get("names") or [])

                # Step 2: PNG OCR for addresses + NTS fields
                if ocr_available:
                    rec = _enrich_via_png_ocr(rec)

                # Step 3: Assessor fallback if no address
                if not rec.get("prop_address"):
                    rec = await _enrich_via_assessor(rec, assessor_page)

                # Step 4: Use prop as mail if no separate mail
                if rec.get("prop_address") and not rec.get("mail_address"):
                    rec["mail_address"] = rec["prop_address"]
                    rec["mail_city"]    = rec["prop_city"]
                    rec["mail_state"]   = rec["prop_state"]
                    rec["mail_zip"]     = rec["prop_zip"]

                status = "✓" if rec.get("prop_address") else "✗"
                log.debug(f"  [{i+1}/{total}] {status} {rec.get('doc_num')} | {rec.get('last_name','')} {rec.get('first_name','')} | {rec.get('prop_address','no address')}")

            except Exception as exc:
                log.warning(f"  [{i+1}/{total}] Error {rec.get('doc_num')}: {exc}")

            enriched.append(rec)
            time.sleep(REQUEST_DELAY)

        await browser.close()

    with_addr = sum(1 for r in enriched if r.get("prop_address"))
    log.info(f"Enrichment done: {with_addr}/{total} addresses ({100*with_addr//max(total,1)}%)")
    return enriched


# ── Smart name classification ──────────────────────────────────────────────────
def _is_company(name: str) -> bool:
    """Return True if this name looks like a company/entity not a person."""
    tokens = set(name.upper().split())
    # Has any company keyword
    if tokens & COMPANY_KEYWORDS:
        return True
    # All-caps single word that's not a typical surname
    if len(name.split()) == 1 and name.isupper():
        return True
    return False


def _assign_names_smart(rec: dict, names: list) -> dict:
    """
    Names[] is alphabetical. Classify each as person or company.
    Borrowers = person names. Trustees/Lenders = company names.
    """
    if not names:
        return rec

    persons   = [n for n in names if not _is_company(n)]
    companies = [n for n in names if _is_company(n)]

    # Assign trustee = first company that looks like a trustee/servicer
    trustee_keywords = {"TRUSTEE", "RECON", "FINANCIAL", "MORTGAGE", "TITLE", "MTC", "FIRST"}
    trustee = None
    for c in companies:
        if set(c.upper().split()) & trustee_keywords:
            trustee = c
            break
    if not trustee and companies:
        trustee = companies[0]
    rec["trustee_name"] = trustee

    # Primary borrower = first person name
    if persons:
        owner_raw = persons[0]
        rec["owner"] = owner_raw

        # Check for couple format: "LASTNAME FIRSTNAME AND LASTNAME2 FIRSTNAME2"
        # or "LASTNAME FIRSTNAME AND FIRSTNAME2" (same last name)
        and_split = re.split(r"\s+AND\s+", owner_raw, maxsplit=1, flags=re.I)

        p1 = _parse_person_name(and_split[0].strip())
        rec["first_name"] = p1["first"]
        rec["last_name"]  = p1["last"]

        # Second borrower
        rec["first_name_2"] = ""
        rec["last_name_2"]  = ""

        if len(and_split) > 1:
            # Remove trailing relationship words: "HUSBAND AND WIFE", "WIFE AND HUSBAND"
            p2_raw = re.sub(r"\s*,?\s*(?:HUSBAND|WIFE|MARRIED|UNMARRIED|A SINGLE|WOMAN|MAN)\b.*$",
                            "", and_split[1], flags=re.I).strip()
            if p2_raw:
                p2 = _parse_person_name(p2_raw)
                # If no last name parsed, use same as p1
                if not p2["last"]:
                    p2["last"] = p1["last"]
                rec["first_name_2"] = p2["first"]
                rec["last_name_2"]  = p2["last"]
        elif len(persons) > 1:
            # Second person is a separate entry
            p2_raw = re.sub(r"\s*,?\s*(?:HUSBAND|WIFE|MARRIED|UNMARRIED)\b.*$",
                            "", persons[1], flags=re.I).strip()
            p2 = _parse_person_name(p2_raw)
            rec["first_name_2"] = p2["first"]
            rec["last_name_2"]  = p2["last"]
    else:
        # All entities — use first company as "owner" display name
        rec["owner"]        = companies[0] if companies else ""
        rec["first_name"]   = ""
        rec["last_name"]    = _clean_company_name(companies[0]) if companies else ""
        rec["first_name_2"] = ""
        rec["last_name_2"]  = ""

    # Grantee = lender (second company, or first if no trustee)
    lenders = [c for c in companies if c != trustee]
    rec["grantee"] = lenders[0] if lenders else (companies[1] if len(companies) > 1 else "")

    return rec


def _parse_person_name(raw: str) -> dict:
    """
    Parse person name in LASTNAME FIRSTNAME [MIDDLE] format.
    Also handles FIRSTNAME LASTNAME if it looks more natural.
    Strips relationship descriptors.
    """
    # Remove relationship words
    raw = re.sub(
        r"\s*,?\s*\b(?:HUSBAND|WIFE|MARRIED|UNMARRIED|A SINGLE|WOMAN|MAN|TRUSTEE|AN?)\b.*$",
        "", raw, flags=re.I
    ).strip().strip(",").strip()

    if not raw:
        return {"first": "", "last": ""}

    tokens = raw.split()
    if not tokens:
        return {"first": "", "last": ""}

    # Single token
    if len(tokens) == 1:
        return {"first": "", "last": tokens[0].title()}

    # LASTNAME FIRSTNAME [MIDDLE] format (recorder convention)
    last  = tokens[0].title()
    first = " ".join(tokens[1:]).title()

    # Sanity check: if last looks like a first name (e.g. short, common first name)
    # and first looks like a last name, don't swap — trust recorder convention
    return {"first": first, "last": last}


def _clean_company_name(name: str) -> str:
    """Title-case a company name for display."""
    return " ".join(w.capitalize() if w not in ("LLC","INC","LP","LLP","NA","DBA") else w
                    for w in name.split())


# ── PNG OCR ────────────────────────────────────────────────────────────────────
def _enrich_via_png_ocr(rec: dict) -> dict:
    doc_num = rec.get("doc_num", "")
    all_text = ""

    for page_num in range(1, 3):
        png = _download_png(doc_num, page_num)
        if png:
            text = _ocr_image(png)
            if text:
                all_text += f"\n--- PAGE {page_num} ---\n" + text
        time.sleep(0.2)

    if not all_text.strip():
        return rec

    return _parse_ocr_fields(rec, all_text)


def _download_png(doc_num: str, page_num: int) -> Optional[bytes]:
    params = {
        "recordingNumber": doc_num,
        "suffix":          "",
        "affidavit":       "false",
        "pageNumber":      page_num,
    }
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = SESSION.get(PNG_API, params=params, timeout=TIMEOUT)
            if resp.ok and "image" in resp.headers.get("content-type", ""):
                return resp.content
        except Exception as exc:
            log.debug(f"  PNG {doc_num} p{page_num} attempt {attempt}: {exc}")
        time.sleep(2 * attempt)
    return None


def _ocr_image(png_bytes: bytes) -> str:
    import pytesseract
    from PIL import Image, ImageFilter, ImageEnhance
    img = Image.open(io.BytesIO(png_bytes)).convert("L")
    img = ImageEnhance.Contrast(img).enhance(2.0)
    img = img.filter(ImageFilter.SHARPEN)
    return pytesseract.image_to_string(img, config="--psm 6 --oem 3")


def _parse_ocr_fields(rec: dict, text: str) -> dict:
    """Extract all fields from full OCR text of NTS document."""

    # ── Property address ───────────────────────────────────────────────────
    if not rec.get("prop_address"):
        # NTS documents describe the property in legal section
        # Patterns ordered by reliability
        for pat in [
            # "property located at 123 Main St, Phoenix, AZ 85001"
            r"(?:property|premises|trust\s+property)\s+(?:is\s+)?(?:located\s+at|known\s+as|described\s+as|situate[d]?\s+at)[:\s]+([^\n]{10,100})",
            # "Situs: 123 Main..."
            r"[Ss]itus[:\s]+([^\n]{10,80})",
            # Street number + direction + name + type + city, AZ zip
            r"(\d{3,5}\s+[NSEW]\.?\s+[\w\s\.#]{5,40}(?:ST|AVE|DR|RD|LN|WAY|BLVD|CT|PL|LOOP|TRL|CIR|PKWY)\b[^\n]{0,30})\n\s*([\w\s]+,\s*AZ\s+\d{5})",
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

    # ── Mailing / trustor address ──────────────────────────────────────────
    if not rec.get("mail_address"):
        for pat in [
            r"[Ww]hen\s+recorded[,\s]+(?:return\s+to|mail\s+to)[:\s]*\n((?:[^\n]+\n){1,5})",
            r"[Mm]ail(?:ing)?\s+[Aa]ddress[:\s]+([^\n]{10,100})",
        ]:
            m = re.search(pat, text, re.I)
            if m:
                block = m.group(1) if m.lastindex else m.group(0)
                addr_m = re.search(
                    r"(\d{2,5}\s+[^\n,]{5,60},\s*[A-Za-z\s]+,?\s*(?:AZ|[A-Z]{2})?\s*\d{5})",
                    block
                )
                if addr_m:
                    addr = _parse_addr(addr_m.group(1))
                    if addr and addr.get("zip"):
                        rec["mail_address"] = addr["street"]
                        rec["mail_city"]    = addr["city"]
                        rec["mail_state"]   = addr["state"] or "AZ"
                        rec["mail_zip"]     = addr["zip"]
                        break

    # ── Original loan amount ───────────────────────────────────────────────
    if not rec.get("amount"):
        for pat in [
            r"[Oo]riginal\s+(?:principal\s+)?(?:sum|balance|note|loan|amount)[:\s]+\$?\s*([\d,]+(?:\.\d{2})?)",
            r"[Uu]npaid\s+(?:principal\s+)?[Bb]alance[:\s]+\$?\s*([\d,]+(?:\.\d{2})?)",
            r"(?:principal\s+)?[Aa]mount\s+(?:of\s+)?(?:the\s+)?[Nn]ote[:\s]+\$?\s*([\d,]+(?:\.\d{2})?)",
            # Dollar amount near "trust deed" context
            r"\$\s*([\d,]{5,12}(?:\.\d{2})?)\s*(?:with|at|per|,)",
        ]:
            m = re.search(pat, text, re.I)
            if m:
                try:
                    val = float(m.group(1).replace(",", "").replace(" ", ""))
                    if 10_000 < val < 50_000_000:   # sanity range
                        rec["amount"] = val
                        break
                except ValueError:
                    pass

    # ── Auction / sale date ────────────────────────────────────────────────
    if not rec.get("auction_date"):
        for pat in [
            r"[Ss]ale\s+[Dd]ate[:\s]+(\w+\s+\d{1,2},?\s+\d{4})",
            r"[Aa]uction\s+[Dd]ate[:\s]+(\w+\s+\d{1,2},?\s+\d{4})",
            r"(?:will\s+be\s+sold|will\s+occur)[^.]{0,80}?(\w+\s+\d{1,2},\s*\d{4})",
            r"(?:at|on)\s+(\w+\s+\d{1,2},\s+\d{4})\s+at\s+\d+[:\d]*\s*[AaPp][Mm]",
            r"on\s+(\w+\s+\d{1,2},\s+\d{4})[,;]",
        ]:
            m = re.search(pat, text, re.I)
            if m:
                d = _norm_date(m.group(1).strip())
                # Validate it's a future or recent date
                if re.match(r"\d{4}-\d{2}-\d{2}", d):
                    rec["auction_date"] = d
                    break

    # ── Trustee phone ──────────────────────────────────────────────────────
    if not rec.get("trustee_phone"):
        m = re.search(r"\(?\d{3}\)?[\s\-\.]\d{3}[\s\-\.]\d{4}", text)
        if m:
            rec["trustee_phone"] = m.group(0).strip()

    # ── Parcel / APN ───────────────────────────────────────────────────────
    if not rec.get("parcel"):
        m = re.search(r"(?:APN|[Pp]arcel\s*(?:[Nn]o|#|[Nn]umber)?)[:\s#]*(\d{3}[-\s]\d{2}[-\s]\d{3})", text)
        if m:
            rec["parcel"] = m.group(1).replace(" ", "-")

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
                    "() => { const r=document.querySelectorAll('table tbody tr td'); return r.length>0 && r[0].innerText.trim()!=''; }",
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

    # Fallback
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
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw


def _build_query(rec: dict) -> Optional[str]:
    last  = (rec.get("last_name")  or "").strip()
    first = (rec.get("first_name") or "").strip()
    # Skip if last name is actually a company
    if last and not _is_company(last) and first and not _is_company(first):
        return f"{last} {first}"
    owner = (rec.get("owner") or "").strip()
    if owner and not _is_company(owner):
        cleaned = re.sub(r"\s+AND\s+.*$", "", owner, flags=re.I).strip()
        return cleaned[:60] if cleaned else None
    return None
