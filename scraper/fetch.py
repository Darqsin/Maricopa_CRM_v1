"""
scraper/fetch.py
Maricopa County Motivated Seller Lead Scraper — Production v1.0
Orchestrates: clerk portal scrape → enrichment → scoring → output
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

# ── local imports ─────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
from clerk_scraper import ClerkScraper
from enricher import enrich_records
from scorer import score_record
from exporter import save_json, export_ghl_csv

# ── logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("scraper/scraper.log", mode="a"),
    ],
)
log = logging.getLogger("fetch")

# ── config ────────────────────────────────────────────────────────────────────
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", 7))

LEAD_TYPES = {
    "NS": ("NOTS", "Notice of Trustee Sale"),
    "FL": ("LIEN", "Federal Tax Lien"),
    "SL": ("LIEN", "State Tax Lien"),
    "DE": ("TAX",  "Tax Deed"),
    "PD": ("PRO",  "Probate Document"),
    "PJ": ("PRO",  "Probate Document"),
}

OUTPUT_PATHS = [
    Path("dashboard/records.json"),
    Path("data/records.json"),
]

GHL_CSV_PATH = Path("data/ghl_export.csv")


# ── helpers ───────────────────────────────────────────────────────────────────
def date_range(lookback: int):
    end   = datetime.utcnow().date()
    start = end - timedelta(days=lookback)
    return str(start), str(end)


def build_output(records: list, start: str, end: str) -> dict:
    with_addr = sum(1 for r in records if r.get("prop_address"))
    return {
        "fetched_at":   datetime.utcnow().isoformat() + "Z",
        "source":       "Maricopa County Recorder",
        "date_range":   {"start": start, "end": end},
        "total":        len(records),
        "with_address": with_addr,
        "records":      records,
    }


# ── main ──────────────────────────────────────────────────────────────────────
async def main():
    start_date, end_date = date_range(LOOKBACK_DAYS)
    log.info(f"Run started | lookback={LOOKBACK_DAYS}d | {start_date} → {end_date}")

    # ── 1. scrape clerk portal ─────────────────────────────────────────────
    scraper = ClerkScraper(lead_types=LEAD_TYPES, start_date=start_date, end_date=end_date)
    raw_records = await scraper.run()
    log.info(f"Clerk scrape complete — {len(raw_records)} raw records")

    # ── 2. enrich (mail address, parcel, auction date, pdf url) ───────────
    enriched = enrich_records(raw_records)
    log.info(f"Enrichment complete — {len(enriched)} records")

    # ── 3. score each record ───────────────────────────────────────────────
    for rec in enriched:
        rec["score"] = score_record(rec)
        rec["flags"] = build_flags(rec)

    # sort by score descending
    enriched.sort(key=lambda r: r.get("score", 0), reverse=True)

    # ── 4. save JSON outputs ───────────────────────────────────────────────
    output = build_output(enriched, start_date, end_date)
    for path in OUTPUT_PATHS:
        save_json(output, path)
        log.info(f"Saved → {path}")

    # ── 5. GHL CSV export ──────────────────────────────────────────────────
    export_ghl_csv(enriched, GHL_CSV_PATH)
    log.info(f"GHL CSV → {GHL_CSV_PATH}")

    log.info(
        f"Run complete | total={len(enriched)} "
        f"| with_addr={output['with_address']} "
        f"| top_score={enriched[0].get('score', 0) if enriched else 0}"
    )
    return output


def build_flags(rec: dict) -> list:
    flags = []
    if rec.get("doc_type") in ("NOTS",):
        flags.append("TRUSTEE_SALE")
    if rec.get("amount") and float(rec.get("amount") or 0) > 500_000:
        flags.append("HIGH_VALUE")
    if rec.get("auction_date"):
        try:
            auc = datetime.strptime(rec["auction_date"], "%Y-%m-%d").date()
            if (auc - datetime.utcnow().date()).days <= 30:
                flags.append("AUCTION_SOON")
        except Exception:
            pass
    if rec.get("prop_address") and rec.get("mail_address"):
        if rec["prop_address"].upper() != rec["mail_address"].upper():
            flags.append("ABSENTEE_OWNER")
    if not rec.get("prop_address"):
        flags.append("NO_ADDRESS")
    return flags


if __name__ == "__main__":
    asyncio.run(main())
