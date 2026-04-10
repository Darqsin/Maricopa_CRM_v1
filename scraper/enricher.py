"""
scraper/enricher.py
Enriches raw clerk records with:
  - Mailing address (from document detail page)
  - Property address (from detail / parcel lookup)
  - Parcel number
  - Trustee name & phone (NTS specific)
  - Auction date (NTS specific)
  - PDF document URL
Uses requests + BeautifulSoup with retry logic. Never crashes on bad records.
"""

import logging
import re
import time
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("enricher")

MAX_RETRIES   = 3
RETRY_DELAY   = 2   # seconds
REQUEST_DELAY = 0.4  # polite delay between requests
TIMEOUT       = 15

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
})

BASE_URL = "https://recorder.maricopa.gov"


def enrich_records(records: list[dict]) -> list[dict]:
    enriched = []
    total = len(records)
    for i, rec in enumerate(records):
        log.debug(f"Enriching {i+1}/{total}: {rec.get('doc_num')}")
        try:
            rec = _enrich_one(rec)
        except Exception as exc:
            log.warning(f"Enrichment failed for {rec.get('doc_num')}: {exc}")
        enriched.append(rec)
        time.sleep(REQUEST_DELAY)
    log.info(f"Enriched {len(enriched)} records")
    return enriched


def _enrich_one(rec: dict) -> dict:
    clerk_url = rec.get("clerk_url")
    if not clerk_url:
        return rec

    html = _fetch(clerk_url)
    if not html:
        return rec

    soup = BeautifulSoup(html, "lxml")

    # ── parcel number ──────────────────────────────────────────────────────
    rec["parcel"] = rec.get("parcel") or _extract_parcel(soup, html)

    # ── PDF url ────────────────────────────────────────────────────────────
    rec["pdf_url"] = rec.get("pdf_url") or _extract_pdf_url(soup, clerk_url)

    # ── mailing address ────────────────────────────────────────────────────
    mail = _extract_address_block(soup, "mail")
    if mail:
        rec["mail_address"] = mail.get("address")
        rec["mail_city"]    = mail.get("city")
        rec["mail_state"]   = mail.get("state")
        rec["mail_zip"]     = mail.get("zip")

    # ── property address ───────────────────────────────────────────────────
    prop = _extract_address_block(soup, "prop")
    if prop:
        rec["prop_address"] = prop.get("address")
        rec["prop_city"]    = prop.get("city")
        rec["prop_state"]   = prop.get("state") or "AZ"
        rec["prop_zip"]     = prop.get("zip")

    # Fallback: if only one address found use for both
    if rec.get("mail_address") and not rec.get("prop_address"):
        rec["prop_address"] = rec["mail_address"]
        rec["prop_city"]    = rec["mail_city"]
        rec["prop_state"]   = rec["mail_state"] or "AZ"
        rec["prop_zip"]     = rec["mail_zip"]

    # ── owner name parsing ─────────────────────────────────────────────────
    owner_parsed = _parse_owner_name(rec.get("owner", ""))
    rec.update(owner_parsed)

    grantee_parsed = _parse_owner_name(rec.get("grantee", ""), prefix="grantee_")
    rec.update(grantee_parsed)

    # ── NTS-specific: trustee + auction date ───────────────────────────────
    if rec.get("cat") == "NOTS" or rec.get("lead_key") == "NS":
        nts = _extract_nts_fields(soup, html)
        rec["trustee_name"]  = rec.get("trustee_name")  or nts.get("trustee_name")
        rec["trustee_phone"] = rec.get("trustee_phone") or nts.get("trustee_phone")
        rec["auction_date"]  = rec.get("auction_date")  or nts.get("auction_date")
        rec["amount"]        = rec.get("amount")        or nts.get("loan_amount")

    return rec


# ── HTTP fetch with retry ──────────────────────────────────────────────────────
def _fetch(url: str) -> Optional[str]:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = SESSION.get(url, timeout=TIMEOUT)
            if resp.ok:
                return resp.text
            log.debug(f"HTTP {resp.status_code} for {url}")
        except requests.RequestException as exc:
            log.debug(f"Request attempt {attempt} failed: {exc}")
        time.sleep(RETRY_DELAY * attempt)
    return None


# ── parcel extraction ──────────────────────────────────────────────────────────
def _extract_parcel(soup: BeautifulSoup, raw_html: str) -> Optional[str]:
    # Maricopa parcel format: XXX-XX-XXX or XXXXXXXXX
    patterns = [
        r"\b(\d{3}-\d{2}-\d{3}[A-Z]?)\b",
        r"\b(\d{3}\s\d{2}\s\d{3})\b",
        r"[Pp]arcel[:\s#]+([0-9\-\s]{9,14})",
        r"APN[:\s]+([0-9\-]{9,14})",
    ]
    for pat in patterns:
        m = re.search(pat, raw_html)
        if m:
            return m.group(1).replace(" ", "-").strip()

    # Try labeled fields
    for label in soup.find_all(string=re.compile(r"[Pp]arcel|APN")):
        parent = label.parent
        if parent:
            sibling = parent.find_next_sibling()
            if sibling:
                text = sibling.get_text(strip=True)
                m = re.search(r"[\d\-]{7,14}", text)
                if m:
                    return m.group(0)
    return None


# ── PDF URL extraction ─────────────────────────────────────────────────────────
def _extract_pdf_url(soup: BeautifulSoup, base_url: str) -> Optional[str]:
    # Look for direct PDF links
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.lower().endswith(".pdf") or "pdf" in href.lower():
            return urljoin(base_url, href)

    # Look for "View Document" or "View Image" links
    for a in soup.find_all("a"):
        text = a.get_text(strip=True).lower()
        if any(kw in text for kw in ("view document", "view image", "view pdf", "download")):
            href = a.get("href", "")
            if href:
                return urljoin(base_url, href)

    # Look for iframe src
    for iframe in soup.find_all("iframe", src=True):
        src = iframe["src"]
        if "pdf" in src.lower() or "image" in src.lower():
            return urljoin(base_url, src)

    return None


# ── address block extraction ───────────────────────────────────────────────────
def _extract_address_block(soup: BeautifulSoup, kind: str) -> Optional[dict]:
    """
    kind: "mail" or "prop"
    Searches labeled sections for address components.
    """
    label_patterns = {
        "mail": re.compile(r"mail(ing)?\s*(address)?", re.I),
        "prop": re.compile(r"prop(erty)?\s*(address|location)?|situs", re.I),
    }
    pat = label_patterns.get(kind, re.compile(kind, re.I))

    # Strategy 1: find label, grab next sibling content
    for label_el in soup.find_all(string=pat):
        parent = label_el.parent
        container = parent.parent if parent else None
        if container:
            block = _parse_address_from_text(container.get_text(separator=" "))
            if block:
                return block

    # Strategy 2: look for table cells with label
    for td in soup.find_all("td"):
        text = td.get_text(strip=True)
        if pat.search(text):
            nxt = td.find_next_sibling("td")
            if nxt:
                block = _parse_address_from_text(nxt.get_text(separator=" "))
                if block:
                    return block

    # Strategy 3: look for div/span with data-* or id hints
    for attr_val in (f"{kind}-address", f"{kind}Address", f"{kind}_address"):
        el = soup.find(id=re.compile(attr_val, re.I))
        if el:
            block = _parse_address_from_text(el.get_text(separator=" "))
            if block:
                return block

    return None


def _parse_address_from_text(text: str) -> Optional[dict]:
    """Extract street/city/state/zip from a text block."""
    text = " ".join(text.split())

    # Pattern: 123 Main St, Phoenix, AZ 85001
    m = re.search(
        r"(\d+\s+[A-Za-z0-9\s\.#,\-]+?),\s*([A-Za-z\s]+),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)",
        text,
    )
    if m:
        return {
            "address": m.group(1).strip(),
            "city":    m.group(2).strip(),
            "state":   m.group(3).strip(),
            "zip":     m.group(4).strip(),
        }

    # Pattern without comma separation
    m2 = re.search(
        r"(\d+\s[\w\s\.#]+)\s([A-Za-z ]+)\s([A-Z]{2})\s(\d{5})",
        text,
    )
    if m2:
        return {
            "address": m2.group(1).strip(),
            "city":    m2.group(2).strip(),
            "state":   m2.group(3).strip(),
            "zip":     m2.group(4).strip(),
        }

    return None


# ── owner name parsing ─────────────────────────────────────────────────────────
SUFFIXES = {"JR", "SR", "II", "III", "IV", "TRUST", "LLC", "CORP", "INC", "LP", "LLP"}

def _parse_owner_name(raw: str, prefix: str = "") -> dict:
    """
    Split 'SMITH JOHN W & JONES MARY' into first/last for two owners.
    Returns dict with keys: first_name, last_name, first_name_2, last_name_2 (with optional prefix).
    """
    result = {
        f"{prefix}first_name":   "",
        f"{prefix}last_name":    "",
        f"{prefix}first_name_2": "",
        f"{prefix}last_name_2":  "",
    }
    if not raw:
        return result

    # Split on & or AND
    parts = re.split(r"\s+(?:&|AND)\s+", raw.strip(), maxsplit=1)

    def parse_one(s: str):
        tokens = s.upper().split()
        if not tokens:
            return "", ""
        # Remove suffixes
        while tokens and tokens[-1] in SUFFIXES:
            tokens.pop()
        if len(tokens) == 1:
            return "", tokens[0]
        # Assume LASTNAME FIRSTNAME [MIDDLE]
        last  = tokens[0]
        first = " ".join(tokens[1:])
        return first.title(), last.title()

    first, last = parse_one(parts[0])
    result[f"{prefix}first_name"] = first
    result[f"{prefix}last_name"]  = last

    if len(parts) > 1:
        first2, last2 = parse_one(parts[1])
        result[f"{prefix}first_name_2"] = first2
        result[f"{prefix}last_name_2"]  = last2

    return result


# ── NTS-specific extraction ────────────────────────────────────────────────────
def _extract_nts_fields(soup: BeautifulSoup, raw_html: str) -> dict:
    out = {
        "trustee_name":  None,
        "trustee_phone": None,
        "auction_date":  None,
        "loan_amount":   None,
    }

    text = soup.get_text(separator=" ")

    # Trustee name: "Trustee: XYZ Trustee Services"
    m = re.search(r"[Tt]rustee[:\s]+([A-Za-z][A-Za-z0-9\s\.,&]+?)(?:\n|Phone|Tel|$)", text)
    if m:
        out["trustee_name"] = m.group(1).strip()[:100]

    # Trustee phone
    m = re.search(r"(?:Phone|Tel|Ph)[:\s]*\(?\d{3}\)?[\s\-\.]\d{3}[\s\-\.]\d{4}", text)
    if m:
        out["trustee_phone"] = re.search(
            r"[\(]?\d{3}[\)\s\-\.]+\d{3}[\s\-\.]\d{4}", m.group(0)
        ).group(0) if re.search(r"[\(]?\d{3}[\)\s\-\.]+\d{3}[\s\-\.]\d{4}", m.group(0)) else None

    # Auction / sale date
    date_patterns = [
        r"[Ss]ale\s+[Dd]ate[:\s]+(\w+\s+\d{1,2},?\s+\d{4})",
        r"[Aa]uction\s+[Dd]ate[:\s]+(\w+\s+\d{1,2},?\s+\d{4})",
        r"[Tt]rustee.{1,10}[Ss]ale.{1,30}(\w+\s+\d{1,2},?\s+\d{4})",
        r"(\d{1,2}/\d{1,2}/\d{4})",
    ]
    for pat in date_patterns:
        m = re.search(pat, text)
        if m:
            raw_date = m.group(1).strip()
            out["auction_date"] = _norm_date_flexible(raw_date)
            break

    # Loan amount
    m = re.search(r"[Oo]riginal\s+[Ll]oan[:\s]+\$?([\d,]+(?:\.\d{2})?)", text)
    if m:
        try:
            out["loan_amount"] = float(m.group(1).replace(",", ""))
        except ValueError:
            pass

    return out


def _norm_date_flexible(raw: str) -> str:
    import dateutil.parser
    try:
        return dateutil.parser.parse(raw).strftime("%Y-%m-%d")
    except Exception:
        pass
    # Manual formats
    for fmt in ("%m/%d/%Y", "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%m-%d-%Y"):
        try:
            from datetime import datetime
            return datetime.strptime(raw.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw
