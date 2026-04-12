"""
scraper/fetch.py — Orchestrator (NS-only)

Date range priority:
  1. START_DATE + END_DATE env vars (set via workflow_dispatch inputs)
  2. LOOKBACK_DAYS env var (default 7)
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

LEAD_TYPES = {
    "NS": ("NOTS", "Notice of Trustee Sale"),
}

OUTPUT_PATHS = [Path("dashboard/records.json"), Path("data/records.json")]
GHL_CSV_PATH = Path("data/ghl_export.csv")


def get_date_range() -> tuple[str, str]:
    """
    Returns (start_date, end_date) as YYYY-MM-DD strings.
    Reads START_DATE/END_DATE env vars first; falls back to LOOKBACK_DAYS.
    """
    start = os.getenv("START_DATE", "").strip()
    end   = os.getenv("END_DATE",   "").strip()

    if start and end:
        # Validate format
        try:
            datetime.strptime(start, "%Y-%m-%d")
            datetime.strptime(end,   "%Y-%m-%d")
            log.info(f"Using explicit date range: {start} → {end}")
            return start, end
        except ValueError:
            log.warning(f"Invalid date format (START_DATE={start}, END_DATE={end}) — falling back to LOOKBACK_DAYS")

    if start and not end:
        end = str(datetime.utcnow().date())
        try:
            datetime.strptime(start, "%Y-%m-%d")
            log.info(f"Using START_DATE={start} to today={end}")
            return start, end
        except ValueError:
            log.warning(f"Invalid START_DATE={start} — falling back to LOOKBACK_DAYS")

    # Fallback: LOOKBACK_DAYS
    lookback = int(os.getenv("LOOKBACK_DAYS", "7"))
    end   = str(datetime.utcnow().date())
    start = str((datetime.utcnow() - timedelta(days=lookback)).date())
    log.info(f"Using lookback={lookback}d: {start} → {end}")
    return start, end


def build_flags(rec):
    flags = ["TRUSTEE_SALE"]
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
    start_date, end_date = get_date_range()
    log.info(f"Run started | {start_date} → {end_date}")

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
        f"| top_score={enriched[0].get('score', 0) if enriched else 0}"
    )


if __name__ == "__main__":
    asyncio.run(main())
