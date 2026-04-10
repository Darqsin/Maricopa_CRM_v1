"""
scraper/enricher.py  v2 — uses Maricopa public API for detail lookup.

Confirmed detail API:
  GET https://publicapi.recorder.maricopa.gov/documents/{recordingNumber}
  Returns: {
    "names": ["CLEAR RECON CORP", "FINANCE OF AMERICA REVERSE LLC", "JAROS FRANK"],
    "documentCodes": ["N/TR SALE"],
    "recordingDate": "4-03-2026",
    "recordingNumber": "20260196990",
    "pageAmount": 3,
    "restricted": false
  }

Name order for NTS: [0]=Trustee, [1]=Lender/Beneficiary, [2]=Trustor/Owner
For other doc types names[0] is typically the grantor/owner.
"""

import logging
import re
import time
from typing import Optional

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("enricher")

API_BASE      = "https://publicapi.recorder.maricopa.gov"
PORTAL_BASE   = "https://recorder.maricopa.gov"
MAX_RETRIES   = 3
RETRY_DELAY   = 2
REQUEST_DELAY = 0.35
TIMEOUT       = 15

SUFFIXES = {"JR", "SR", "II", "III", "IV", "TRUST", "LLC", "CORP", "INC", "LP", "LLP"}

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept":  "application/json, */*",
    "Referer": "https://recorder.maricopa.gov/",
})


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
    return enriched


def _enrich_one(rec: dict) -> dict:
    doc_num = rec.get("doc_num")
    if not doc_num:
        return rec

    # ── 1. Fetch detail from public API ───────────────────────────────────
    detail = _fetch_json(f"{API_BASE}/documents/{doc_num}")
    if detail:
        names = detail.get("names") or []

        # Parse names by lead type
        if rec.get("lead_key") == "NS":
            # NTS order: [Trustee, Lender, Owner/Trustor, ...]
            if len(names) >= 1:
                rec["trustee_name"] = names[0]
            if len(names) >= 3:
                owner_raw = names[2]
            elif len(names) >= 2:
                owner_raw = names[1]
            else:
                owner_raw = names[0] if names else ""
            rec["owner"] = owner_raw
        else:
            # For liens/deeds/probate: first name is typically the owner
            rec["owner"]   = names[0] if len(names) > 0 else rec.get("owner", "")
            rec["grantee"] = names[1] if len(names) > 1 else rec.get("grantee", "")

        # Parse first/last names from owner
        name_parsed = _parse_owner_name(rec.get("owner", ""))
        rec.update(name_parsed)

        if len(names) > 1 and rec.get("lead_key") != "NS":
            name2 = _parse_owner_name(names[1], prefix="2_")
            rec["first_name_2"] = name2.get("2_first_name", "")
            rec["last_name_2"]  = name2.get("2_last_name", "")

        rec["restricted"] = detail.get("restricted", False)

    # ── 2. Fetch the document detail HTML page for address + NTS fields ───
    detail_html_url = f"{PORTAL_BASE}/recording/document-details?id={doc_num}"
    html = _fetch_html(detail_html_url)
    if html:
        soup = BeautifulSoup(html, "lxml")
        text = soup.get_text(separator=" ")

        # Property / mailing address
        prop = _extract_address(soup, text, "prop")
        if prop:
            rec["prop_address"] = prop.get("address")
            rec["prop_city"]    = prop.get("city")
            rec["prop_state"]   = prop.get("state") or "AZ"
            rec["prop_zip"]     = prop.get("zip")

        mail = _extract_address(soup, text, "mail")
        if mail:
            rec["mail_address"] = mail.get("address")
            rec["mail_city"]    = mail.get("city")
            rec["mail_state"]   = mail.get("state")
            rec["mail_zip"]     = mail.get("zip")

        # If only one address found, use for both
        if rec.get("prop_address") and not rec.get("mail_address"):
            rec["mail_address"] = rec["prop_address"]
            rec["mail_city"]    = rec["prop_city"]
            rec["mail_state"]   = rec["prop_state"]
            rec["mail_zip"]     = rec["prop_zip"]

        # Parcel number
        rec["parcel"] = rec.get("parcel") or _extract_parcel(text)

        # NTS-specific fields
        if rec.get("lead_key") == "NS":
            nts = _extract_nts(text)
            rec["trustee_phone"] = rec.get("trustee_phone") or nts.get("phone")
            rec["auction_date"]  = rec.get("auction_date")  or nts.get("auction_date")
            rec["amount"]        = rec.get("amount")        or nts.get("loan_amount")
            if nts.get("trustee_name") and not rec.get("trustee_name"):
                rec["trustee_name"] = nts["trustee_name"]

        # Amount from lien docs
        if not rec.get("amount"):
            rec["amount"] = _extract_amount(text)

    return rec


# ── HTTP helpers ───────────────────────────────────────────────────────────────
def _fetch_json(url: str) -> Optional[dict]:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = SESSION.get(url, timeout=TIMEOUT)
            if resp.ok:
                return resp.json()
        except Exception as exc:
            log.debug(f"JSON fetch attempt {attempt} failed for {url}: {exc}")
        time.sleep(RETRY_DELAY * attempt)
    return None


def _fetch_html(url: str) -> Optional[str]:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = SESSION.get(url, timeout=TIMEOUT)
            if resp.ok:
                return resp.text
        except Exception as exc:
            log.debug(f"HTML fetch attempt {attempt} failed for {url}: {exc}")
        time.sleep(RETRY_DELAY * attempt)
    return None


# ── parsers ────────────────────────────────────────────────────────────────────
def _extract_parcel(text: str) -> Optional[str]:
    patterns = [
        r"\b(\d{3}-\d{2}-\d{3}[A-Z]?)\b",
        r"[Pp]arcel[:\s#]+([0-9\-]{9,14})",
        r"APN[:\s]+([0-9\-]{9,14})",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return m.group(1).strip()
    return None


def _extract_address(soup: BeautifulSoup, text: str, kind: str) -> Optional[dict]:
    patterns = {
        "prop": re.compile(r"prop(erty)?\s*(address|location)?|situs", re.I),
        "mail": re.compile(r"mail(ing)?\s*(address)?", re.I),
    }
    pat = patterns.get(kind, re.compile(kind, re.I))

    for label in soup.find_all(string=pat):
        container = label.parent
        if container:
            block = container.find_next_sibling() or container.parent
            if block:
                addr = _parse_address_text(block.get_text(separator=" "))
                if addr:
                    return addr

    for td in soup.find_all("td"):
        if pat.search(td.get_text(strip=True)):
            nxt = td.find_next_sibling("td")
            if nxt:
                addr = _parse_address_text(nxt.get_text(separator=" "))
                if addr:
                    return addr

    return None


def _parse_address_text(text: str) -> Optional[dict]:
    text = " ".join(text.split())
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
    return None


def _parse_owner_name(raw: str, prefix: str = "") -> dict:
    result = {
        f"{prefix}first_name": "",
        f"{prefix}last_name":  "",
    }
    if not raw:
        return result
    tokens = raw.upper().split()
    while tokens and tokens[-1] in SUFFIXES:
        tokens.pop()
    if not tokens:
        return result
    if len(tokens) == 1:
        result[f"{prefix}last_name"] = tokens[0].title()
    else:
        result[f"{prefix}last_name"]  = tokens[0].title()
        result[f"{prefix}first_name"] = " ".join(tokens[1:]).title()
    return result


def _extract_nts(text: str) -> dict:
    out: dict = {}

    m = re.search(
        r"[Tt]rustee[:\s]+([A-Za-z][A-Za-z0-9\s\.,&]{3,80}?)(?:Phone|Tel|\n|$)", text
    )
    if m:
        out["trustee_name"] = m.group(1).strip()[:100]

    m = re.search(r"\(?\d{3}\)?[\s\-\.]\d{3}[\s\-\.]\d{4}", text)
    if m:
        out["phone"] = m.group(0).strip()

    for pat in [
        r"[Ss]ale\s+[Dd]ate[:\s]+(\w+\s+\d{1,2},?\s+\d{4})",
        r"[Aa]uction\s+[Dd]ate[:\s]+(\w+\s+\d{1,2},?\s+\d{4})",
        r"(\d{1,2}/\d{1,2}/\d{4})",
    ]:
        m = re.search(pat, text)
        if m:
            out["auction_date"] = _norm_date_flex(m.group(1))
            break

    m = re.search(
        r"[Oo]riginal\s+[Ll]oan[:\s]+\$?([\d,]+(?:\.\d{2})?)", text
    )
    if m:
        try:
            out["loan_amount"] = float(m.group(1).replace(",", ""))
        except ValueError:
            pass

    return out


def _extract_amount(text: str) -> Optional[float]:
    matches = re.findall(r"\$[\d,]+(?:\.\d{2})?", text)
    if matches:
        try:
            return float(matches[0].replace("$", "").replace(",", ""))
        except ValueError:
            pass
    return None


def _norm_date_flex(raw: str) -> str:
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%m/%d/%Y", "%m-%d-%Y", "%B %d %Y"):
        try:
            from datetime import datetime
            return datetime.strptime(raw.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw
