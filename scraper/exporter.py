"""
scraper/exporter.py
Handles all output: JSON saves and GHL CSV export.
"""

import csv
import json
import logging
from datetime import datetime
from pathlib import Path

log = logging.getLogger("exporter")

GHL_COLUMNS = [
    "First Name",
    "Last Name",
    "Mailing Address",
    "Mailing City",
    "Mailing State",
    "Mailing Zip",
    "Property Address",
    "Property City",
    "Property State",
    "Property Zip",
    "Lead Type",
    "Document Type",
    "Date Filed",
    "Document Number",
    "Original Loan",
    "Trustee Name",
    "Trustee Phone",
    "Auction Date",
    "PDF URL",
]


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
                row = _rec_to_ghl_row(rec)
                writer.writerow(row)
                rows_written += 1
            except Exception as exc:
                log.warning(f"GHL row error for {rec.get('doc_num')}: {exc}")

    log.info(f"GHL CSV → {path} ({rows_written} rows)")


def _rec_to_ghl_row(rec: dict) -> dict:
    lead_type_labels = {
        "NS": "Notice of Trustee Sale",
        "FL": "Federal Tax Lien",
        "SL": "State Tax Lien",
        "DE": "Tax Deed",
        "PD": "Probate Document",
        "PJ": "Probate Document",
    }

    first = rec.get("first_name") or _first_from_owner(rec.get("owner", ""))
    last  = rec.get("last_name")  or _last_from_owner(rec.get("owner", ""))

    amount = rec.get("amount")
    loan_str = f"${float(amount):,.2f}" if amount else ""

    return {
        "First Name":       first,
        "Last Name":        last,
        "Mailing Address":  rec.get("mail_address") or "",
        "Mailing City":     rec.get("mail_city")    or "",
        "Mailing State":    rec.get("mail_state")   or "",
        "Mailing Zip":      rec.get("mail_zip")     or "",
        "Property Address": rec.get("prop_address") or "",
        "Property City":    rec.get("prop_city")    or "",
        "Property State":   rec.get("prop_state")   or "AZ",
        "Property Zip":     rec.get("prop_zip")     or "",
        "Lead Type":        lead_type_labels.get(rec.get("lead_key", ""), rec.get("cat_label", "")),
        "Document Type":    rec.get("doc_type")     or "",
        "Date Filed":       rec.get("filed")        or "",
        "Document Number":  rec.get("doc_num")      or "",
        "Original Loan":    loan_str,
        "Trustee Name":     rec.get("trustee_name")  or "",
        "Trustee Phone":    rec.get("trustee_phone") or "",
        "Auction Date":     rec.get("auction_date")  or "",
        "PDF URL":          rec.get("pdf_url")       or "",
    }


def _first_from_owner(owner: str) -> str:
    parts = owner.strip().split()
    if len(parts) >= 2:
        return parts[1].title()
    return ""


def _last_from_owner(owner: str) -> str:
    parts = owner.strip().split()
    if parts:
        return parts[0].title()
    return ""
