"""
scraper/exporter.py  v2

Changes from v1:
- _normalize_doc_type(): cleans up raw document_type values that clerk_scraper
  writes into records before they reach GHL (e.g. "NOTICE OF TRUSTEE'S SALE"
  → "NTS").  Purely defensive — current output already strips these columns,
  but this ensures the dashboard JSON and any future column additions stay clean.
- _rec_to_row(): no column changes; Lead Type / Document Type remain excluded
  via GHL_COLUMNS + _BANNED_COLUMNS.

CSV columns:
- Removed: Lead Type, Document Type
- Added:   First Name 2, Last Name 2
- Added:   Estimated Value, Equity  (blank — filled manually later)
"""

import csv
import json
import logging
from pathlib import Path

log = logging.getLogger("exporter")

GHL_COLUMNS = [
    "First Name",
    "Last Name",
    "First Name 2",
    "Last Name 2",
    "Mailing Address",
    "Mailing City",
    "Mailing State",
    "Mailing Zip",
    "Property Address",
    "Property City",
    "Property State",
    "Property Zip",
    "Date Filed",
    "Document Number",
    "Original Loan",
    "Estimated Value",
    "Equity",
    "Trustee Name",
    "Trustee Phone",
    "Auction Date",
    "PDF URL",
]

# Keys that must never appear in output regardless of what upstream writes
_BANNED_COLUMNS = {"lead_type", "Lead Type", "document_type", "Document Type"}

# ── Document-type normalisation ───────────────────────────────────────────────
# Maps substrings (upper-cased) found in clerk_scraper's raw doc-type field
# to the canonical short form used in the dashboard / GHL.
_DOC_TYPE_MAP = {
    "NOTICE OF TRUSTEE":  "NTS",   # covers "NOTICE OF TRUSTEE'S SALE" etc.
    "TRUSTEE SALE":       "NTS",
    "NOTICE OF DEFAULT":  "NOD",
    "NOTICE OF SALE":     "NTS",
    "SUBSTITUTION OF":    "SOT",   # Substitution of Trustee
    "DEED OF TRUST":      "DOT",
    "RECONVEYANCE":       "RECON",
    "RELEASE":            "REL",
    "ASSIGNMENT":         "ASSIGN",
}

_LEAD_TYPE_MAP = {
    "NTS":    "Trustee Sale",
    "NOD":    "Notice of Default",
    "SOT":    "Sub of Trustee",
    "DOT":    "Deed of Trust",
    "RECON":  "Reconveyance",
    "REL":    "Release",
    "ASSIGN": "Assignment",
}


def _normalize_doc_type(raw: str) -> str:
    """Return a canonical short code for *raw* document type."""
    upper = (raw or "").strip().upper()
    for substr, code in _DOC_TYPE_MAP.items():
        if substr in upper:
            return code
    # Already a short code?
    if upper in _LEAD_TYPE_MAP:
        return upper
    return raw.strip().title() if raw else ""


def _derive_lead_type(doc_type_code: str) -> str:
    return _LEAD_TYPE_MAP.get(doc_type_code.upper(), doc_type_code)


# ── I/O helpers ───────────────────────────────────────────────────────────────

def save_json(data: dict, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
    log.info(f"JSON saved → {path} ({len(data.get('records', []))} records)")


def export_ghl_csv(records: list[dict], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows_written = 0
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=GHL_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for rec in records:
            try:
                row = _rec_to_row(rec)
                row = {k: v for k, v in row.items() if k not in _BANNED_COLUMNS}
                writer.writerow(row)
                rows_written += 1
            except Exception as exc:
                log.warning(f"Row error {rec.get('doc_num')}: {exc}")
    log.info(f"GHL CSV → {path} ({rows_written} rows)")


def _rec_to_row(rec: dict) -> dict:
    amount = rec.get("amount")
    try:
        loan_str = f"${float(amount):,.2f}" if amount else ""
    except (ValueError, TypeError):
        loan_str = str(amount or "").strip()

    # Normalise doc type stored in the record (defensive; these columns are
    # not in GHL_COLUMNS but keep the JSON payload clean for the dashboard).
    raw_doc_type = rec.get("document_type") or rec.get("doc_type") or ""
    rec["_doc_type_code"] = _normalize_doc_type(raw_doc_type)

    return {
        "First Name":       (rec.get("first_name")    or "").strip(),
        "Last Name":        (rec.get("last_name")     or "").strip(),
        "First Name 2":     (rec.get("first_name_2")  or "").strip(),
        "Last Name 2":      (rec.get("last_name_2")   or "").strip(),
        "Mailing Address":  (rec.get("mail_address")  or "").strip(),
        "Mailing City":     (rec.get("mail_city")     or "").strip(),
        "Mailing State":    (rec.get("mail_state")    or "").strip(),
        "Mailing Zip":      (rec.get("mail_zip")      or "").strip(),
        "Property Address": (rec.get("prop_address")  or "").strip(),
        "Property City":    (rec.get("prop_city")     or "").strip(),
        "Property State":   (rec.get("prop_state")    or "AZ").strip(),
        "Property Zip":     (rec.get("prop_zip")      or "").strip(),
        "Date Filed":       (rec.get("filed")         or "").strip(),
        "Document Number":  (rec.get("doc_num")       or "").strip(),
        "Original Loan":    loan_str,
        "Estimated Value":  "",
        "Equity":           "",
        "Trustee Name":     (rec.get("trustee_name")  or "").strip(),
        "Trustee Phone":    (rec.get("trustee_phone") or "").strip(),
        "Auction Date":     (rec.get("auction_date")  or "").strip(),
        "PDF URL":          (rec.get("pdf_url")       or "").strip(),
    }
