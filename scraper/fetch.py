"""
scraper/fetch.py — Orchestrator (NS-only)
"""
import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from clerk_scraper import ClerkScraper
from enricher import enrich_records
from scorer import score_record
from exporter import save_json, export_ghl_csv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("scraper/scraper.log", mode="a"),
    ],
)
log = logging.getLogger("fetch")

LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", 7))

# NS ONLY per user request
LEAD_TYPES = {
    "NS": ("NOTS", "Notice of Trustee Sale"),
}

OUTPUT_PATHS = [Path("dashboard/records.json"), Path("data/records.json")]
GHL_CSV_PATH = Path("data/ghl_export.csv")


def date_range(lookback):
    end   = datetime.utcnow().date()
    start = end - timedelta(days=lookback)
    return str(start), str(end)


def build_flags(rec):
    flags = []
    flags.append("TRUSTEE_SALE")
    if rec.get("amount") and float(rec.get("amount") or 0) > 500_000:
        flags.append("HIGH_VALUE")
    if rec.get("auction_date"):
        try:
            auc  = datetime.strptime(rec["auction_date"], "%Y-%m-%d").date()
            days = (auc - datetime.utcnow().date()).days
            if 0 <= days <= 30:
                flags.append("AUCTION_SOON")
            elif 0 <= days <= 60:
                flags.append("AUCTION_60_DAYS")
        except Exception:
            pass
    if rec.get("prop_address") and rec.get("mail_address"):
        if rec["prop_address"].strip().upper() != rec["mail_address"].strip().upper():
            flags.append("ABSENTEE_OWNER")
    if not rec.get("prop_address"):
        flags.append("NO_ADDRESS")
    return flags


async def main():
    start_date, end_date = date_range(LOOKBACK_DAYS)
    log.info(f"Run started | lookback={LOOKBACK_DAYS}d | {start_date} → {end_date}")

    scraper = ClerkScraper(lead_types=LEAD_TYPES, start_date=start_date, end_date=end_date)
    raw_records = await scraper.run()
    log.info(f"Clerk scrape: {len(raw_records)} raw records")

    enriched = await enrich_records(raw_records)
    log.info(f"Enrichment: {len(enriched)} records")

    for rec in enriched:
        rec["score"] = score_record(rec)
        rec["flags"] = build_flags(rec)
    enriched.sort(key=lambda r: r.get("score", 0), reverse=True)

    output = {
        "fetched_at":   datetime.utcnow().isoformat() + "Z",
        "source":       "Maricopa County Recorder — Notice of Trustee Sale",
        "date_range":   {"start": start_date, "end": end_date},
        "total":        len(enriched),
        "with_address": sum(1 for r in enriched if r.get("prop_address")),
        "records":      enriched,
    }
    for path in OUTPUT_PATHS:
        save_json(output, path)
        log.info(f"Saved → {path}")

    export_ghl_csv(enriched, GHL_CSV_PATH)
    log.info(f"GHL CSV → {GHL_CSV_PATH}")
    log.info(
        f"Done | total={output['total']} | with_addr={output['with_address']} "
        f"| top_score={enriched[0].get('score',0) if enriched else 0}"
    )


if __name__ == "__main__":
    asyncio.run(main())
