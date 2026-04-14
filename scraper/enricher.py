"""
scraper/enricher.py  v14

Changes from v13:
- TRUSTEE_OFFICE_RE: new constant covering Clear Recon Corp / 3707 E Southern
  and other known foreclosure-mill office addresses.  Applied in both
  _parse_addr() and the prop_address extraction loop so the office address
  can never bleed in from any code-path.
- _is_inline_format(): detects single-line "Ronald Herb / Tolesoa" style NTS
  docs (trustee + trustor packed onto one line, total < 15 non-blank lines).
- _extract_all_fields(): when _is_inline_format() is True, routes through a
  dedicated inline parser that uses full-line regex instead of block-anchored
  patterns, and skips the trustee-address block search entirely.
- _extract_raw_trustee_inline() / _extract_raw_trustor_inline(): new helpers
  for single-line format.
- _is_trustee_office_address(): new guard called before any address is
  accepted as the property address.
- scrape.yml fix is in a separate file (scrape.yml.patch).

Architecture (unchanged):
- OCR runs first; recorder API fills only what OCR missed.
- Raw trustor/trustee strings saved to rec["raw_trustor"] / rec["raw_trustee"].
- _is_trustee_like()         — central check: trustee vs owner?
- _extract_raw_trustor()/_extract_raw_trustee() — isolated extractors
- _set_owner_from_trustor()  — writes all owner/name fields
- _assign_names_smart()      — API fallback
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

PNG_API      = "https://publicapi.recorder.maricopa.gov/preview/image"
RECORDER_API = "https://publicapi.recorder.maricopa.gov"
ASSESSOR_BASE = "https://mcassessor.maricopa.gov"
PORTAL_BASE   = "https://recorder.maricopa.gov"

REQUEST_DELAY = 0.3
TIMEOUT       = 20
MAX_RETRIES   = 2

# ── Courthouse / court building — never use as property address ───────────────
COURTHOUSE_PATTERNS = [
    r"201\s+W(?:est)?\.?\s+Jefferson",
    r"Superior\s+Court\s+Building",
    r"Maricopa\s+County\s+Courthouse",
    r"Superior\s+Building",
    r"Maricopa\s+County\s+Court",
    r"Phoenix,\s+Arizona\s+85012",   # Quality/MTC address fragment
]
COURTHOUSE_RE = re.compile("|".join(COURTHOUSE_PATTERNS), re.I)

# ── Trustee office / foreclosure-mill addresses ───────────────────────────────
# These are the physical offices of trustee companies.  They appear in the
# "Name, Address & Telephone of Trustee" block but must NEVER be used as the
# property address.  The v13 version had some of these inside COURTHOUSE_RE,
# but that conflated two semantically distinct concepts and missed variations.
TRUSTEE_OFFICE_PATTERNS = [
    # Clear Recon Corp — the #1 bleeder in Maricopa NTS docs
    r"3707\s+E(?:ast)?\.?\s+Southern\s+(?:Ave(?:nue)?|Rd|Road)?",
    r"Clear\s+Recon\s+Corp",
    # Quality Loan Service / MTC Financial
    r"2763\s+(?:Camino\s+Del\s+)?Rio\s+(?:South|Norte)",
    r"Quality\s+Loan\s+Service",
    r"MTC\s+Financial",
    # Western Progressive
    r"1 First American Way",
    r"Western\s+Progressive",
    # Barrett Daffin Frappier
    r"4004\s+Belt\s+Line",
    r"Barrett\s+Daffin",
    # T.D. Service / Trustee Corps
    r"Trustee\s+Corps",
    r"T\.D\.\s+Service",
    # ZBS Law
    r"ZBS\s+Law",
    r"3765\s+(?:La\s+)?Mission",
    # Generic: any line whose first identifiable token is "Trustee" or "Commissioner"
    r"^\s*(?:Trustee|Foreclosure\s+Commissioner)\s+Corp",
]
TRUSTEE_OFFICE_RE = re.compile("|".join(TRUSTEE_OFFICE_PATTERNS), re.I)


def _is_trustee_office_address(addr: str) -> bool:
    """Return True when *addr* is a known foreclosure-mill office address."""
    return bool(TRUSTEE_OFFICE_RE.search(addr)) or bool(COURTHOUSE_RE.search(addr))


# ── US states ─────────────────────────────────────────────────────────────────
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
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
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


# ── Top-level entry point ─────────────────────────────────────────────────────

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
    total    = len(records)
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
            await assessor_page.goto(
                f"{ASSESSOR_BASE}/", wait_until="domcontentloaded", timeout=20_000
            )
            await asyncio.sleep(1)
        except Exception:
            pass

        for i, rec in enumerate(records):
            try:
                if ocr_ok:
                    rec = _enrich_via_ocr(rec)
                detail = _fetch_recorder_detail(rec.get("doc_num", ""))
                if detail:
                    rec = _assign_names_smart(rec, detail.get("names") or [])
                if not rec.get("prop_address"):
                    rec = await _enrich_via_assessor(rec, assessor_page)
                log.debug(
                    f"  [{i+1}/{total}] {'✓' if rec.get('prop_address') else '✗'} "
                    f"{rec.get('doc_num')} | {rec.get('last_name','')} "
                    f"{rec.get('first_name','')} "
                    f"| 2nd: {rec.get('last_name_2','')} {rec.get('first_name_2','')}"
                    f"| {rec.get('prop_address','no address')}"
                )
            except Exception as exc:
                log.warning(f"  [{i+1}/{total}] Error {rec.get('doc_num')}: {exc}")
            enriched.append(rec)
            time.sleep(REQUEST_DELAY)

        await browser.close()

    with_addr = sum(1 for r in enriched if r.get("prop_address"))
    log.info(f"Done: {with_addr}/{total} addresses ({100*with_addr//max(total,1)}%)")
    return enriched


# ── OCR pipeline ──────────────────────────────────────────────────────────────

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
            resp = SESSION.get(
                PNG_API,
                params={
                    "recordingNumber": doc_num,
                    "suffix":          "",
                    "affidavit":       "false",
                    "pageNumber":      page_num,
                },
                timeout=TIMEOUT,
            )
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


# ── Inline-format detection ───────────────────────────────────────────────────

def _is_inline_format(text: str) -> bool:
    """
    Return True for single-line / condensed NTS format
    (e.g. Ronald Herb docs, Tolesoa docs).

    Indicators:
    - Fewer than 15 non-blank lines in the full OCR output, OR
    - Trustee and trustor appear on the same line (separated by / or ;), OR
    - Key labels appear mid-line rather than at line-start
      ("NAME AND ADDRESS OF TRUSTOR: ... NAME AND ADDRESS OF TRUSTEE: ...")
    """
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if len(lines) < 15:
        return True
    # Trustee + trustor share a line
    if any(
        re.search(r"trustee.{0,60}trustor|trustor.{0,60}trustee", l, re.I)
        for l in lines
    ):
        return True
    # Multiple ALL-CAPS label-value pairs on one line
    if any(
        re.search(
            r"NAME\s+AND\s+ADDRESS\s+OF\s+TRUSTOR.{5,200}NAME\s+AND\s+ADDRESS\s+OF\s+TRUSTEE",
            l, re.I,
        )
        for l in lines
    ):
        return True
    return False


# ── Trustee / trustor helpers ─────────────────────────────────────────────────

_TRUSTEE_COMPANY_KW = {
    "TRUSTEE","RECON","SERVICING","MORTGAGE","FINANCIAL","TITLE","MTC",
    "COMMISSIONER","OFFICES","LAW","CORPS","PLLC","ESQ","PRIME","QUALITY",
    "PIONEER","CLEAR","STATEWIDE","FORECLOSURE","LAKEVIEW","SHELLPOINT",
    "FREEDOM","NEWREZ","PENNYMAC","CARRINGTON","PLANET","ROCKET","GUILD",
    "DESERT","NAVY","CLEARWATER","NOVA","IGLOO","SENECA","CITIZENS","WELLS",
    "FARGO","CHASE","LENDERS","LENDING","NATIONSTAR","LOANDEPOT","BARRETT",
    "DAFFIN","FRAPPIER","ZBS","ONITY","VYLLA","PRESTIGE","AMRESCO",
}


def _is_trustee_like(name: str) -> bool:
    """Return True when *name* looks like a trustee/servicer, not a property owner."""
    if not name:
        return False
    upper  = name.upper().strip()
    tokens = set(re.split(r"[\s,\.]+", upper))
    if tokens & _TRUSTEE_COMPANY_KW:
        return True
    if re.search(r"\bESQ\.?\b|\bPLLC\b", upper):
        return True
    if re.match(r"in\s+favor\s+of|in\s+re\b", upper):
        return True
    if re.match(r"^-{2,}|^page\s+\d+|^scanner$", upper):
        return True
    return False


# ── OCR label / boilerplate junk guards ──────────────────────────────────────

_TRUSTOR_JUNK_RE = re.compile(
    r"^(?:"
    r"name\s+and\s+address"            # section label text
    r"|address\s+of\s+the\s+beneficiary"
    r"|as\s+shown\s+on\s+the\s+deed"
    r"|beneficiary"
    r"|name\s+address"
    r"|name\b"                          # bare "Name" OCR label
    r"|\{[^}]*\}"                       # curly-brace boilerplate e.g. "{As Shown On...}"
    r"|page\s+\d+"
    r"|scanner"
    r")",
    re.I,
)


def _is_junk_name(s: str) -> bool:
    """Return True when *s* looks like OCR label text or boilerplate, not a real name."""
    s = s.strip()
    if not s or len(s) < 2:
        return True
    if _TRUSTOR_JUNK_RE.match(s):
        return True
    if "{" in s or "}" in s:         # curly braces anywhere = boilerplate
        return True
    if re.match(r"^[\.\,\;\:\!\?]+$", s):   # pure punctuation
        return True
    return False


def _extract_raw_trustee(text: str) -> str:
    """
    Locate the NAME, ADDRESS & TELEPHONE NUMBER OF TRUSTEE block
    and return the first meaningful (non-address) line.
    Returns "" on failure.
    """
    block_m = re.search(
        r"NAME[,\s]+ADDRESS\s*[&]\s*TELEPHONE\s+NUMBER\s+OF\s+TRUSTEE[^\n]*\n"
        r"((?:[^\n]*\n){1,10})",
        text, re.I,
    )
    if not block_m:
        return ""
    bad_words = {
        "SALE","OBJECTION","BELIEVE","DEFENSE","ACTION","COURT","FILE",
        "LICENSED","BROKER","QUALIFICATIONS","REGULATION","AGENCY",
        "FAX","SALES","ONLINE","INFORMATION","AVAILABLE","REQUESTS","WEBSITE",
        # OCR label artifacts seen in Maricopa docs
        "PHONE","NUMBER","UNOFFICIAL","DOCUMENT","REGEN",
    }
    for line in block_m.group(1).splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("(") or line.startswith("["):
            continue
        if set(line.upper().split()) & bad_words:
            continue
        if re.match(r"^\d{3,5}\s+\w", line) or re.match(r"^\(?\d{3}\)?", line):
            continue   # street address or phone
        if re.match(r"^[A-Za-z][A-Za-z\s]+,\s*[A-Z]{2}\s+\d{5}", line):
            continue   # bare city/state/zip
        line = re.sub(r"^[\|\[\]\{\}]+\s*", "", line).strip()   # leading pipe/bracket artifact
        line = re.sub(r",?\s+a\s+member\s+of\s+the\s+State\s+Bar.*$", "", line, flags=re.I).strip()
        line = re.sub(r",?\s+licensed\s+real\s+estate\s+broker.*$",    "", line, flags=re.I).strip()
        if len(line) > 3:
            words = line.split()
            half  = len(words) // 2
            if half > 1 and words[:half] == words[half:]:
                line = " ".join(words[:half])
            return line[:100]
    return ""


def _extract_raw_trustee_inline(text: str) -> str:
    """
    Trustee extraction for inline / single-line NTS format.
    Tries mid-line patterns before falling back to the standard block search.
    """
    # Pattern: "NAME AND ADDRESS OF TRUSTEE (...): <NAME>, licensed..."
    for pat in [
        r"NAME\s+AND\s+ADDRESS\s+OF\s+TRUSTEE[^:]*[:\s]+([^:,\n]+?)(?:,\s*licensed|,\s*an\s+az|\s+QUALIFICATIONS|\s+\d{3,5}\s+[NSEW]|\Z)",
        r"[Ss]ubstitute\s+[Tt]rustee[:\s]+([^\n,]{3,80}?)(?:,|\n)",
        r"([A-Z][A-Za-z]{2,}(?:\s+[A-Za-z\.]+){1,4})\s+as\s+[Ff]oreclosure\s+[Cc]ommissioner",
    ]:
        m = re.search(pat, text, re.I)
        if m:
            candidate = " ".join(m.group(1).split()).strip()
            cut = re.search(
                r",?\s+(?:licensed|real\s+estate|broker|dba\s+\w|qualif"
                r"|\d{3}[\-\.]|an\s+arizona|irvine|glendale|phoenix"
                r"|scottsdale|tempe|mesa|chandler|gilbert|peoria"
                r"|surprise|avondale|goodyear|buckeye|ca\s+\d|az\s+\d)",
                candidate, re.I,
            )
            if cut:
                candidate = candidate[:cut.start()].strip().rstrip(",")
            if len(candidate) > 3:
                return candidate[:100]
    return _extract_raw_trustee(text)


def _extract_raw_trustor(text: str) -> str:
    """
    Locate the 'Name and address of original trustor' block.
    Returns the raw name string (may include two names joined by AND).
    """
    block_m = re.search(
        r"[Nn]ame\s+and\s+[Aa]ddress\s+of\s+(?:original\s+)?[Tt]rustor[:\s]*\n"
        r"((?:[^\n]*\n){1,12})",
        text,
    )
    if block_m:
        tlines = [l.strip() for l in block_m.group(1).splitlines() if l.strip()]
        for idx, tl in enumerate(tlines):
            if tl.startswith("(") or tl.startswith("["):
                continue
            if _is_junk_name(tl):
                continue
            if re.match(r"^[A-Za-z][A-Za-z\s]+,\s*[A-Z]{2}\s+\d{5}", tl):
                continue   # bare city/state/zip
            if re.match(r"^\d{2,5}\s+", tl):
                continue   # street address line
            result = tl
            if re.search(r"\bAND\s*$", tl, re.I) and idx + 1 < len(tlines):
                nxt = tlines[idx + 1]
                if not re.match(r"^\d{2,5}\s+", nxt):
                    result = tl.rstrip() + " " + nxt
            return result

    # Fallback inline patterns
    for pat in [
        r"NAME\s+AND\s+ADDRESS\s+OF\s+TRUSTOR[:\s]+([^:]+?)"
        r"(?:\s+BENEFICIARY\b|\s+ORIGINAL\s+PRINCIPAL|\s+TAX\s+PARCEL"
        r"|\s+IDENTIFIABLE|\s+NAME\s+AND\s+ADDRESS\s+OF\s+TRUSTEE|\Z)",
        r"executed\s+by\s+([A-Z][a-zA-Z\s\.,]+?)\s*,?\s*"
        r"(?:[Aa]\s+[Ss]ingle|[Aa]\s+[Mm]arried|[Hh]usband|[Ww]ife"
        r"|as\s+[Tt]rustor|[Tt]rustor\b)",
        r"([A-Z][a-zA-Z\s\.,]{3,60}?)\s+as\s+[Tt]rustor",
        r"[Tt]rustor[:\s]+([A-Z][A-Za-z\s,\.]{3,80}?)(?:\n|,\s*[Aa]\s+[Ss]ingle)",
        r"[Gg]rantor[:\s]+([A-Z][A-Za-z\s,\.]{3,80}?)(?:\n|,)",
        r"[Bb]orrower[:\s]+([A-Z][A-Za-z\s,\.]{3,80}?)(?:\n|,)",
    ]:
        m = re.search(pat, text, re.I)
        if m:
            candidate = m.group(1).strip()
            if not _is_junk_name(candidate):
                return candidate
    return ""


def _extract_raw_trustor_inline(text: str) -> str:
    """
    Trustor extraction for inline / single-line NTS format.
    Prioritises mid-line patterns, then delegates to _extract_raw_trustor().
    """
    # Direct inline label pattern first
    m = re.search(
        r"NAME\s+AND\s+ADDRESS\s+OF\s+TRUSTOR[:\s]+([^:]+?)"
        r"(?:\s+(?:BENEFICIARY|ORIGINAL\s+PRINCIPAL|TAX\s+PARCEL"
        r"|IDENTIFIABLE|NAME\s+AND\s+ADDRESS\s+OF\s+TRUSTEE)|\Z)",
        text, re.I,
    )
    if m:
        raw = m.group(1).strip()
        # Strip embedded address tail: "SMITH JOHN 1234 N Main St Phoenix AZ 85001"
        addr_start = re.search(r",?\s*\d{2,5}\s+[NSEW\d]", raw)
        if addr_start:
            raw = raw[:addr_start.start()].strip().rstrip(",")
        if raw:
            return raw
    return _extract_raw_trustor(text)


_REL_PAT = re.compile(
    r"\s*,?\s*\b(?:husband|wife|married|unmarried|a\s+single|woman|man|"
    r"trustor|grantor|an?|not|nor|joint\s+tenants|as\s+his\s+sole|"
    r"as\s+her\s+sole|sole\s+and|separate\s+property|with\s+right)\b.*$",
    re.I,
)


def _set_owner_from_trustor(rec: dict, raw_trustor: str, known_trustee: str = "") -> dict:
    """
    Parse *raw_trustor* and write first/last/first_2/last_2/owner into *rec*.
    Guards against trustee contamination and instrument phrases.
    """
    if not raw_trustor:
        return rec
    raw = " ".join(raw_trustor.split()).strip().rstrip(",")
    addr_start = re.search(r",?\s*\d{2,5}\s+[NSEW\d]", raw)
    name_part  = (
        raw[:addr_start.start()].strip().rstrip(",")
        if addr_start
        else raw.split(",")[0].strip()
    )
    if known_trustee and name_part.upper() in known_trustee.upper():
        return rec
    if _is_trustee_like(name_part):
        return rec
    if _is_junk_name(name_part):
        return rec
    if re.match(r"(?:in\s+favor\s+of|in\s+re\b|scanner|page\s+\d)", name_part, re.I):
        return rec

    rec["owner"]       = name_part
    rec["raw_trustor"] = raw_trustor

    is_co = _is_company(name_part)
    if is_co:
        display = re.sub(
            r",?\s*(?:AN?\s+ARIZONA|A\s+\w+\s+)?(?:LIMITED\s+LIABILITY\s+COMPANY|"
            r"LLC|L\.L\.C\.|CORPORATION|CORP|INC\.?|LIMITED\s+PARTNERSHIP|L\.P\.)(?:[,\s].*)?$",
            "", name_part, flags=re.I,
        ).strip().rstrip(",")
        rec["first_name"]   = ""
        rec["last_name"]    = display.title()
        rec["first_name_2"] = ""
        rec["last_name_2"]  = ""
        return rec

    name_clean = re.sub(
        r",?\s+(?:HUSBAND|WIFE)\s+AND\s+", " AND ", name_part, flags=re.I
    )
    # Catch run-together "and" e.g. "Tom Arandaand April N." (lowercase 'and' glued to name,
    # followed by a space then the second name). Only fires when 'and' is preceded by a
    # lowercase letter and followed by whitespace — safe against Amanda/Orlando/Fernando etc.
    name_clean = re.sub(r"(?<=[a-z])and(?=\s)", " AND ", name_clean, flags=re.I)
    name_clean = re.sub(r"\s{2,}", " ", name_clean)   # collapse any double-space artifact
    parts = re.split(r"\s+AND\s+", name_clean, maxsplit=1, flags=re.I)
    p1_raw = _REL_PAT.sub("", parts[0]).strip().strip(",")
    p1 = _parse_person_name(p1_raw, from_doc=True)
    rec["first_name"]   = p1["first"]
    rec["last_name"]    = p1["last"]
    rec["first_name_2"] = ""
    rec["last_name_2"]  = ""
    if len(parts) > 1:
        p2_raw = _REL_PAT.sub("", parts[1]).strip().strip(",")
        if p2_raw:
            p2 = _parse_person_name(p2_raw, from_doc=True)
            if not p2["last"]:
                p2["last"] = p1["last"]
            rec["first_name_2"] = p2["first"]
            rec["last_name_2"]  = p2["last"]
    return rec


def _owner_is_suspect(rec: dict) -> bool:
    """
    Return True when the current owner/name fields look like they were filled
    with a trustee, beneficiary, or junk value.
    """
    last    = (rec.get("last_name") or "").strip()
    first   = (rec.get("first_name") or "").strip()
    owner   = (rec.get("owner") or "").strip()
    trustee = (rec.get("trustee_name") or "").upper().strip()
    if not last:
        return True
    full = f"{first} {last}".strip().upper()
    if trustee and (full in trustee or last.upper() in trustee):
        return True
    if _is_trustee_like(owner or last):
        return True
    if re.match(r"(?:in\s+favor\s+of|in\s+re\b|scanner|page\s+\d|---)", last, re.I):
        return True
    if _is_junk_name(last) or _is_junk_name(first):
        return True
    return False


# ── Main field-extraction dispatcher ─────────────────────────────────────────

def _extract_all_fields(rec: dict, text: str) -> dict:
    """Route to inline or standard extractor based on document format."""
    if _is_inline_format(text):
        log.debug(f"  {rec.get('doc_num')} — inline format detected")
        return _extract_fields_inline(rec, text)
    return _extract_fields_standard(rec, text)


def _extract_fields_inline(rec: dict, text: str) -> dict:
    """
    Field extraction for condensed single-line NTS format
    (Ronald Herb / Tolesoa style).

    Key differences vs standard:
    - Use inline-aware trustee/trustor extractors.
    - Property address: IDENTIFIABLE LOCATION label only — never fall through
      to positional address matching which would pick up the trustee address.
    - Trustee address block is explicitly skipped for property-address purposes.
    """
    # ── TRUSTEE ───────────────────────────────────────────────────────────────
    raw_trustee = _extract_raw_trustee_inline(text)
    if raw_trustee:
        rec["raw_trustee"]  = raw_trustee
        rec["trustee_name"] = raw_trustee

    # ── PROPERTY ADDRESS — inline format: IDENTIFIABLE LOCATION label only ───
    if not rec.get("prop_address"):
        for pat in [
            r"(?:PURPORTED\s+STREET\s+ADDRESS|[Ss]treet\s+address\s+or\s+identifiable\s+location)"
            r"[:\s]+([^\n*\$:]+?)(?:\s+\*|\s+\d{8,}|\s+NAME\s+AND\s+ADDRESS|\Z)",
            r"IDENTIFIABLE\s+LOCATION[:\s]+([^\n*\$:]+?)(?:\s+\*|\s+\d{8,}|\s+NAME\s+AND\s+ADDRESS|\Z)",
            r"IDENTIFIABLE\s+LOCATION[:\s]+([^\n:]+)",
            r"[Cc]ommonly\s+known\s+as[:\s]+([^\n:]+)",
            r"[Ss]itus[:\s]+([^\n:]+)",
        ]:
            m = re.search(pat, text, re.I)
            if not m:
                continue
            raw = m.group(1).strip()
            # Hard-block trustee office addresses
            if _is_trustee_office_address(raw):
                continue
            addr = _parse_addr(raw)
            if addr and addr.get("zip"):
                rec["prop_address"] = addr["street"]
                rec["prop_city"]    = addr["city"]
                rec["prop_state"]   = addr["state"] or "AZ"
                rec["prop_zip"]     = addr["zip"]
                break

    # ── TRUSTOR / OWNER ───────────────────────────────────────────────────────
    raw_trustor = _extract_raw_trustor_inline(text)
    if raw_trustor:
        rec["raw_trustor"] = raw_trustor
        rec = _set_owner_from_trustor(
            rec, raw_trustor, known_trustee=rec.get("trustee_name", "")
        )

    # ── APN, auction date, loan amount, deed of trust ─────────────────────────
    rec = _extract_common_fields(rec, text)
    return rec


def _extract_fields_standard(rec: dict, text: str) -> dict:
    """
    Field extraction for standard multi-line NTS format (unchanged from v13,
    but now calls _is_trustee_office_address() at every address acceptance
    point, and the prop_address loop explicitly guards against trustee office
    addresses for every candidate).
    """
    trustee_name = rec.get("trustee_name", "")

    # ── TRUSTEE ───────────────────────────────────────────────────────────────
    raw_trustee = _extract_raw_trustee(text)
    if raw_trustee:
        rec["raw_trustee"]  = raw_trustee
        rec["trustee_name"] = raw_trustee

    if not rec.get("trustee_name"):
        for pat in [
            r"NAME\s+AND\s+ADDRESS\s+OF\s+TRUSTEE[^:]*[:\s]+([^:,]+?)"
            r"(?:,\s*licensed|,\s*an\s+az|\s+QUALIFICATIONS|\s+\d{3,5}\s+[NSEW]|\Z)",
            r"[Ss]ubstitute\s+[Tt]rustee[:\s]+([^\n,]{3,80}?)(?:,|\n)",
            r"designation\s+of\s+([A-Z][A-Za-z]{2,}(?:\s+[A-Za-z\.]+){1,4})"
            r"\s+as\s+(?:[Ff]oreclosure\s+)?[Cc]ommissioner",
            r"([A-Z][A-Za-z]{2,}(?:\s+[A-Za-z\.]+){1,4})\s+as\s+[Ff]oreclosure\s+[Cc]ommissioner",
        ]:
            m = re.search(pat, text, re.I)
            if m:
                candidate = " ".join(m.group(1).split()).strip()
                cut = re.search(
                    r",?\s+(?:licensed|real\s+estate|broker|dba\s+\w|qualif"
                    r"|\d{3}[\-\.]|an\s+arizona|irvine|glendale|phoenix"
                    r"|scottsdale|tempe|mesa|chandler|gilbert|peoria"
                    r"|surprise|avondale|goodyear|buckeye|ca\s+\d|az\s+\d)",
                    candidate, re.I,
                )
                if cut:
                    candidate = candidate[:cut.start()].strip().rstrip(",")
                bad = {
                    "SALE","OBJECTION","BELIEVE","DEFENSE","ACTION","COURT","FILE",
                    "LICENSED","BROKER","QUALIFICATIONS","REGULATION","AGENCY",
                    "PHONE","NUMBER","UNOFFICIAL","DOCUMENT",
                }
                if set(candidate.upper().split()) & bad:
                    continue
                if len(candidate) > 3:
                    words = candidate.split()
                    half  = len(words) // 2
                    if half > 1 and words[:half] == words[half:]:
                        candidate = " ".join(words[:half])
                    rec["raw_trustee"]  = candidate[:100]
                    rec["trustee_name"] = candidate[:100]
                    break

    trustee_name = rec.get("trustee_name", "")

    # ── TRUSTEE PHONE ─────────────────────────────────────────────────────────
    trustee_block_m = re.search(
        r"NAME[,\s]+ADDRESS\s*[&]\s*TELEPHONE\s+NUMBER\s+OF\s+TRUSTEE[^\n]*\n"
        r"((?:[^\n]*\n){1,10})",
        text, re.I,
    )
    phone_window = (
        text[trustee_block_m.start():trustee_block_m.start() + 600]
        if trustee_block_m
        else text
    )
    phones = re.findall(r"\(?\d{3}\)?[\s\-\.]\d{3}[\s\-\.]\d{4}", phone_window)
    if phones:
        rec["trustee_phone"] = phones[0].strip()

    # ── PROPERTY ADDRESS ──────────────────────────────────────────────────────
    _auction_addr_re = re.compile(
        r"(?:public\s+auction|highest\s+bidder|law\s+(?:office|firm)|PLLC|Esq\.?|sold\s+at)"
        r"[^\n]{0,250}?(\d{3,5}\s+[^\n,]{5,60},\s*[A-Za-z\s]+,\s*(?:AZ|Arizona)\s+\d{5})",
        re.I | re.S,
    )
    _auction_addrs = {am.group(1).strip().upper() for am in _auction_addr_re.finditer(text)}

    if not rec.get("prop_address"):
        for pat in [
            r"(?:PURPORTED\s+STREET\s+ADDRESS|[Ss]treet\s+address\s+or\s+identifiable\s+location)"
            r"[:\s]+([^\n]+)",
            r"IDENTIFIABLE\s+LOCATION[:\s]+([^\n*\$]+?)(?:\s+\*|\s+\d{8,}|\s+NAME\s+AND\s+ADDRESS|\Z)",
            r"IDENTIFIABLE\s+LOCATION[:\s]+([^\n]+)",
            r"[Cc]ommonly\s+known\s+as[:\s]+([^\n]+)",
            r"(?:[Ss]treet\s+address[^:]*)?[Pp]urported\s+to\s+be[:\s]*:?\s*([^\n]{10,100})",
            r"[Ss]treet\s+address[^.]{0,80}?(?:is|be)[:\s]+([^\n]{10,100})",
            r"[Ss]itus[:\s]+([^\n]+)",
            r"(\d{2,5}\s+[NSEW]?\.?\s*[\w\s\.#]{4,50}"
            r"(?:ST|AVE|DR|RD|LN|WAY|BLVD|CT|PL|LOOP|TRL|CIR|PKWY"
            r"|STREET|AVENUE|ROAD|COURT|LANE|DRIVE)\b[^\n]{0,15})\n\s*([\w\s]+,\s*AZ\s+\d{5})",
        ]:
            m = re.search(pat, text, re.I)
            if not m:
                continue
            raw = m.group(1).strip()
            if m.lastindex and m.lastindex > 1:
                raw = raw + ", " + m.group(2).strip()
            raw = re.sub(r"^[Ii]s\s+[Pp]urported\s+[Tt]o\s+[Bb]e[:\s]*", "", raw).strip()

            # ── NEW v14: hard-block trustee office addresses ──────────────────
            if _is_trustee_office_address(raw):
                log.debug(
                    f"  {rec.get('doc_num')} — blocked trustee office addr: {raw!r}"
                )
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

    # ── MAILING ADDRESS ───────────────────────────────────────────────────────
    if not rec.get("mail_address"):
        m = re.search(
            r"(?:[Ww]hen\s+[Rr]ecorded\s+[Mm]ail\s+[Tt]o|WHEN\s+RECORDED\s+MAIL\s+TO)"
            r"[:\s]*\n((?:[^\n]+\n){1,6})",
            text,
        )
        if m:
            block      = m.group(1)
            lines      = [l.strip() for l in block.strip().splitlines() if l.strip()]
            first_line = lines[0] if lines else ""
            if not _is_company(first_line) and not (
                trustee_name and first_line.upper() in trustee_name.upper()
            ):
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

    # ── TRUSTOR / OWNER ───────────────────────────────────────────────────────
    raw_trustor = _extract_raw_trustor(text)
    if raw_trustor:
        rec["raw_trustor"] = raw_trustor
        rec = _set_owner_from_trustor(
            rec, raw_trustor, known_trustee=trustee_name
        )

    # ── APN, auction date, loan amount, deed of trust ─────────────────────────
    rec = _extract_common_fields(rec, text)
    return rec


def _extract_common_fields(rec: dict, text: str) -> dict:
    """
    Fields shared between inline and standard formats:
    APN, auction date, loan amount, deed-of-trust number.
    """
    # ── APN ───────────────────────────────────────────────────────────────────
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

    # ── AUCTION DATE ──────────────────────────────────────────────────────────
    if not rec.get("auction_date"):
        for pat in [
            r"(\w+\s+\d{1,2},?\s+\d{4})\s+at\s+\d{1,2}:\d{2}\s*[APMapm]{2}",
            r"on\s+(\w+\s+\d{1,2},?\s+\d{4})\s+at\s+\d",
            r"(\d{1,2}/\d{1,2}/\d{4})\s+at\s+\d{1,2}:\d{2}\s*[APMapm]{2}",
            r"on\s+(\d{1,2}/\d{1,2}/\d{4})\s+at\s+\d",
            r"(?:SALE\s+)?(?:DATE[:\s]+)?(?:ON\s+)?"
            r"((?:JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST"
            r"|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)\s+\d{1,2},?\s+\d{4})",
            r"((?:JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST"
            r"|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)\s+[\d_]+,?\s+\d{4})",
            r"ON\s+((?:JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST"
            r"|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)\s+\d{1,2})[,\.\s].*?(\d{4})",
            r"[Ss]ale\s+[Dd]ate[:\s]+(\w+\s+\d{1,2},?\s+\d{4})",
            r"notice\s+is\s+hereby\s+given\s+that\s+on\s+(\d{1,2}/\d{1,2}/\d{4})",
        ]:
            m = re.search(pat, text, re.I)
            if not m:
                continue
            raw = (
                (m.group(1).strip() + " " + m.group(2).strip())
                if m.lastindex and m.lastindex >= 2
                else m.group(1).strip()
            )
            if re.search(r"_{2,}", raw):
                continue
            d = _norm_date(raw)
            if re.match(r"20\d{2}-\d{2}-\d{2}", d):
                if int(d[:4]) >= CURRENT_YEAR:
                    rec["auction_date"] = d
                    break

    # ── LOAN AMOUNT ───────────────────────────────────────────────────────────
    existing_amount = rec.get("amount")
    try:
        _has_amount = (
            existing_amount is not None
            and float(str(existing_amount).replace(",","").replace("$","").strip()) > 0
        )
    except (ValueError, TypeError):
        _has_amount = False

    if not _has_amount:
        for pat in [
            r"ORIGINAL\s+PRINCIPAL\s+BALANCE[:\s]+\$?\s*([\d,]+(?:\.\d{2})?)",
            r"[Oo]riginal\s+[Pp]rincipal\s+[Bb]alance[:\s]+\$?\s*([\d,]+(?:\.\d{2})?)",
            r"will\s+bid\s+an\s+estimate\s+of\s+\$([\d,]+\.?\d*)",
            r"reinstat[e\w]*\s+(?:prior[^$]{0,60})?\s*is\s+\$([\d,]+\.?\d*)",
            r"[Oo]riginal\s+(?:principal\s+)?(?:balance|sum|note|indebtedness)"
            r"[:\s]+\$?\s*([\d,]+(?:\.\d{2})?)",
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

    # ── DEED OF TRUST # ───────────────────────────────────────────────────────
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
    # Strip leading/trailing pipe artifacts and brackets
    raw = re.sub(r"^[\|\[\]]+\s*", "", raw).strip()
    raw = re.sub(r"[\[\]]", "", raw).strip()
    raw = re.sub(r"\s*\|+\s*$", "", raw).strip()      # trailing pipe e.g. "123 Main St |"
    # Strip leading 5-digit scanner/page-number prefix: "08093 6302 E. McKellips" → "6302 E. McKellips"
    raw = re.sub(r"^\d{5}\s+(?=\d{3,5}\s+[A-Za-z])", "", raw).strip()
    # Strip 1-3 digit page prefix before a real street number
    raw = re.sub(r"^\d{1,3}\s+(?:[A-Za-z][\w\s\.]{0,50}?)(?=\d{3,5}\s+[NSEW])", "", raw).strip()
    raw = re.sub(r"^\d{1,3}\s+(?=\d{2,5}\s+[NSEW])", "", raw).strip()

    # Hard-block trustee office and courthouse addresses
    if _is_trustee_office_address(raw):
        return None

    def _clean_street(s: str) -> str:
        """Strip trailing pipe/bracket artifacts from a parsed street string."""
        return re.sub(r"\s*[\|\[\]]+\s*$", "", s).strip()

    # "street, city, [state] zip"
    m = re.match(
        r"^(.+?),\s*([A-Za-z][A-Za-z\s]*?),\s*(?:([A-Za-z]{2,})\s+)?(\d{5}(?:-\d{4})?)$",
        raw,
    )
    if m:
        street   = m.group(1).strip()
        city_raw = m.group(2).strip()
        state    = _normalize_state((m.group(3) or "AZ").strip())
        zip_     = m.group(4).strip()
        city     = _fix_city(city_raw)
        if city and not _is_trustee_office_address(city):
            return {"street": _clean_street(street.title()), "city": city.title(), "state": state, "zip": zip_}

    # No comma before city: "123 Main St City, AZ 85001"
    m2 = re.match(r"^(\d+\s+.+?),?\s*([A-Za-z]{2,})\s+(\d{5})$", raw, re.I)
    if m2:
        pre    = m2.group(1).strip()
        state  = _normalize_state(m2.group(2))
        zip_   = m2.group(3)
        street, city = _split_street_city(pre)
        if city and not _is_trustee_office_address(city):
            return {"street": _clean_street(street.title()), "city": city.title(), "state": state, "zip": zip_}
        if pre and not _is_trustee_office_address(pre):
            return {"street": _clean_street(pre.title()), "city": "", "state": state, "zip": zip_}

    # Fallback
    zip_m = re.search(r"(\d{5})", raw)
    if zip_m:
        pre     = raw[:zip_m.start()].rstrip(",").strip()
        state_m = re.search(r",?\s*([A-Za-z]{2,})\s*$", pre)
        state   = _normalize_state(state_m.group(1)) if state_m else "AZ"
        pre     = pre[:state_m.start()].strip() if state_m else pre
        parts   = pre.split(",")
        if len(parts) >= 2:
            street = parts[0].strip()
            city   = _fix_city(parts[-1].strip())
            if city and not _is_trustee_office_address(city):
                return {
                    "street": _clean_street(street.title()),
                    "city":   city.title(),
                    "state":  state,
                    "zip":    zip_m.group(1),
                }
    return None


def _normalize_state(raw: str) -> str:
    raw = raw.strip().upper()
    if raw in US_STATES:
        return raw
    names = {
        "ARIZONA":"AZ","CALIFORNIA":"CA","TEXAS":"TX","NEVADA":"NV",
        "UTAH":"UT","COLORADO":"CO","NEW MEXICO":"NM","OKLAHOMA":"OK",
        "KANSAS":"KS","VIRGINIA":"VA","KENTUCKY":"KY",
    }
    if raw in names:
        return names[raw]
    if raw.startswith("ARIZO"):
        return "AZ"
    for abbr in US_STATES:
        if raw == abbr[:len(raw)] and len(raw) >= 3:
            return abbr
    return "AZ"


def _split_street_city(text: str) -> tuple:
    tokens  = text.upper().split()
    last_st = -1
    for i, tok in enumerate(tokens):
        if tok.rstrip(".").rstrip(",") in STREET_TYPES:
            last_st = i
    if last_st >= 0 and last_st < len(tokens) - 1:
        return " ".join(tokens[:last_st+1]), " ".join(tokens[last_st+1:])
    return text, ""


def _fix_city(city: str) -> str:
    tokens = city.upper().split()
    # Strip street-type word that bled in from address
    for i, tok in enumerate(tokens):
        if tok.rstrip(".") in STREET_TYPES and i < len(tokens) - 1:
            return " ".join(tokens[i+1:]).title()
    # Strip leading unit number that bled in: "236 Scottsdale" → "Scottsdale"
    if tokens and re.match(r"^\d+$", tokens[0]) and len(tokens) > 1:
        return " ".join(tokens[1:]).title()
    return city


# ── Name helpers ──────────────────────────────────────────────────────────────

def _is_company(name: str) -> bool:
    if not name:
        return False
    tokens = set(re.split(r"[\s,\.]+", name.upper()))
    return bool(tokens & COMPANY_WORDS)


def _parse_person_name(raw: str, from_doc: bool = False) -> dict:
    raw = re.sub(
        r"\s*,?\s*\b(?:husband|wife|married|unmarried|a\s+single|woman|man"
        r"|trustor|grantor)\b.*$",
        "", raw, flags=re.I,
    ).strip().strip(",").strip()
    if not raw:
        return {"first": "", "last": ""}

    # Strip trailing generational suffixes so they don't become last name
    raw = re.sub(r",?\s+\b(Jr\.?|Sr\.?|II|III|IV|V)\s*$", "", raw, flags=re.I).strip()

    tokens = raw.split()
    if not tokens:
        return {"first": "", "last": ""}
    if len(tokens) == 1:
        return {"first": "", "last": tokens[0].title()}

    # Mixed-case with real lowercase → natural First Last order (OCR doc text)
    has_lower = any(c.islower() for c in raw)
    if has_lower and from_doc:
        return {"first": " ".join(tokens[:-1]).title(), "last": tokens[-1].title()}

    # API source (from_doc=False): always LAST FIRST [MIDDLE] order
    if not from_doc:
        return {"last": tokens[0].title(), "first": " ".join(tokens[1:]).title()}

    # from_doc=True, ALL-CAPS: natural order FIRST [MIDDLE] LAST
    return {"first": " ".join(tokens[:-1]).title(), "last": tokens[-1].title()}


def _assign_names_smart(rec: dict, names: list) -> dict:
    """Recorder API fallback — runs after OCR."""
    if not names:
        return rec
    names = [
        n for n in names
        if n.strip() and not re.match(r"^-{2,}|^Page\s+\d+|^Scanner$", n.strip(), re.I)
    ]
    if not names:
        return rec
    persons   = [n for n in names if not _is_company(n)]
    companies = [n for n in names if _is_company(n)]

    trustee = next(
        (c for c in companies if _is_trustee_like(c)),
        companies[0] if companies else None,
    )
    if trustee and not rec.get("trustee_name"):
        words = trustee.split()
        half  = len(words) // 2
        if half > 1 and words[:half] == words[half:]:
            trustee = " ".join(words[:half])
        rec["trustee_name"] = trustee

    trustee_str = (rec.get("trustee_name") or "").upper()

    if _owner_is_suspect(rec):
        safe_persons = [
            p for p in persons
            if not _is_trustee_like(p)
            and p.upper() not in trustee_str
            and not re.match(r"(?:in\s+favor\s+of|in\s+re\b)", p, re.I)
        ]
        if safe_persons:
            api_raw = safe_persons[0]
            if len(safe_persons) > 1:
                api_raw = api_raw + " AND " + safe_persons[1]
            rec = _set_owner_from_trustor(rec, api_raw, known_trustee=trustee_str)
            log.debug(f"  API overrode suspect owner for {rec.get('doc_num')} → {api_raw!r}")
        elif companies:
            co = companies[0]
            if not re.match(r"(?:in\s+favor\s+of|in\s+re\b)", co, re.I) and not _is_trustee_like(co):
                rec["owner"]      = co
                rec["first_name"] = ""
                rec["last_name"]  = co.title()

    lenders = [c for c in companies if c != trustee]
    rec["grantee"] = lenders[0] if lenders else ""
    return rec


# ── Assessor fallback ─────────────────────────────────────────────────────────

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
                    "() => { const r=document.querySelectorAll('table tbody tr td');"
                    " return r.length>0&&r[0].innerText.trim()!=''; }",
                    timeout=12_000,
                )
            except Exception:
                pass
            await asyncio.sleep(0.3)
            result = await page.evaluate("""
                () => {
                    for (const row of document.querySelectorAll('table tbody tr')) {
                        const cells=Array.from(row.querySelectorAll('td'))
                            .map(td=>td.innerText.trim());
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
        await page.goto(
            f"{ASSESSOR_BASE}/mcs/?q={apn_digits}",
            wait_until="domcontentloaded", timeout=30_000,
        )
        try:
            await page.wait_for_function(
                "() => document.body.innerText.includes('PROPERTY INFORMATION')"
                "||document.body.innerText.includes('Mailing Address')",
                timeout=12_000,
            )
        except Exception:
            pass
        await asyncio.sleep(0.3)
        text  = await page.evaluate("() => document.body.innerText")
        prop  = _extract_from_text(r"PROPERTY INFORMATION\s*\n([^\n]+)", text)
        mail  = _extract_from_text(r"Mailing Address\s*\n([^\n]+)", text)
        return mail, prop
    except Exception as exc:
        log.debug(f"APN detail {apn}: {exc}")
        return None, None


# ── Recorder API ──────────────────────────────────────────────────────────────

def _fetch_recorder_detail(doc_num: str) -> Optional[dict]:
    if not doc_num:
        return None
    try:
        resp = RECORDER_SESSION.get(
            f"{RECORDER_API}/documents/{doc_num}", timeout=TIMEOUT
        )
        if resp.ok:
            return resp.json()
    except Exception as exc:
        log.debug(f"Recorder detail {doc_num}: {exc}")
    return None


# ── Utilities ─────────────────────────────────────────────────────────────────

def _extract_from_text(pattern: str, text: str) -> Optional[dict]:
    m = re.search(pattern, text)
    return _parse_addr(m.group(1).strip()) if m else None


def _norm_date(raw: str) -> str:
    raw = raw.strip().rstrip(".")
    for fmt in (
        "%m/%d/%Y", "%B %d, %Y", "%b %d, %Y",
        "%B %d %Y", "%b %d %Y", "%m-%d-%Y", "%B %d, %Y",
    ):
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
