"""
scraper/exporter.py — JSON + GHL CSV output

GHL CSV columns (updated):
  - Added: First Name 2, Last Name 2
  - Removed: Lead Type
  - Cleaned: Original Loan (no trailing spaces)
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
    amount = rec.get("amount")
    if amount:
        try:
            loan_str = f"${float(amount):,.2f}"
        except (ValueError, TypeError):
            loan_str = str(amount).strip()
    else:
        loan_str = ""

    return {
        "First Name":       (rec.get("first_name")   or "").strip(),
        "Last Name":        (rec.get("last_name")    or "").strip(),
        "First Name 2":     (rec.get("first_name_2") or "").strip(),
        "Last Name 2":      (rec.get("last_name_2")  or "").strip(),
        "Mailing Address":  (rec.get("mail_address") or "").strip(),
        "Mailing City":     (rec.get("mail_city")    or "").strip(),
        "Mailing State":    (rec.get("mail_state")   or "").strip(),
        "Mailing Zip":      (rec.get("mail_zip")     or "").strip(),
        "Property Address": (rec.get("prop_address") or "").strip(),
        "Property City":    (rec.get("prop_city")    or "").strip(),
        "Property State":   (rec.get("prop_state")   or "AZ").strip(),
        "Property Zip":     (rec.get("prop_zip")     or "").strip(),
        "Document Type":    (rec.get("doc_type")     or "N/TR SALE").strip(),
        "Date Filed":       (rec.get("filed")        or "").strip(),
        "Document Number":  (rec.get("doc_num")      or "").strip(),
        "Original Loan":    loan_str,
        "Trustee Name":     (rec.get("trustee_name")  or "").strip(),
        "Trustee Phone":    (rec.get("trustee_phone") or "").strip(),
        "Auction Date":     (rec.get("auction_date")  or "").strip(),
        "PDF URL":          (rec.get("pdf_url")       or "").strip(),
    }
