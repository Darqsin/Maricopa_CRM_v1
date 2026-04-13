"""
scraper/fetch.py — Orchestrator (NTS-only)

Date range priority:
  1. START_DATE + END_DATE env vars  (workflow_dispatch inputs)
  2. LOOKBACK_DAYS env var           (default 7)

Run modes (controlled by env vars set in scrape.yml):
  SCRAPE_ONLY=1  → scrape clerk only; save raw records.json; skip enrichment.
                   PNGs / PDFs are not yet downloaded at this point.
  ENRICH_ONLY=1  → load existing raw records.json; run OCR + enrichment;
                   export GHL CSV.  PNGs must already exist.
  (neither)      → original behaviour: scrape + enrich in one pass.
                   Use this for local development / manual runs.
"""

import asyncio
import json
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

OUTPUT_PATHS   = [Path("dashboard/records.json"), Path("data/records.json")]
RAW_JSON_PATH  = Path("data/records_raw.json")   # pre-enrichment snapshot
GHL_CSV_PATH   = Path("data/ghl_export.csv")


# ── Date range ────────────────────────────────────────────────────────────────

def get_date_range() -> tuple[str, str]:
    start = os.getenv("START_DATE", "").strip()
    end   = os.getenv("END_DATE",   "").strip()

    if start and end:
        try:
            datetime.strptime(start, "%Y-%m-%d")
            datetime.strptime(end,   "%Y-%m-%d")
            log.info(f"Using explicit date range: {start} → {end}")
            return start, end
        except ValueError:
            log.warning(
                f"Invalid date format (START_DATE={start}, END_DATE={end})"
                " — falling back to LOOKBACK_DAYS"
            )

    if start and not end:
        end = str(datetime.utcnow().date())
        try:
            datetime.strptime(start, "%Y-%m-%d")
            log.info(f"Using START_DATE={start} to today={end}")
            return start, end
        except ValueError:
            log.warning(f"Invalid START_DATE={start} — falling back to LOOKBACK_DAYS")

    lookback = int(os.getenv("LOOKBACK_DAYS", "7"))
    end      = str(datetime.utcnow().date())
    start    = str((datetime.utcnow() - timedelta(days=lookback)).date())
    log.info(f"Using lookback={lookback}d: {start} → {end}")
    return start, end


# ── Flags / scoring ───────────────────────────────────────────────────────────

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


def finalise(enriched: list[dict], start_date: str, end_date: str) -> None:
    """Score, sort, write JSON outputs and GHL CSV."""
    for rec in enriched:
        rec["score"] = score_record(rec)
        rec["flags"] = build_flags(rec)
    enriched.sort(key=lambda r: r.get("score", 0), reverse=True)

    output = {
        "fetched_at":  datetime.utcnow().isoformat() + "Z",
        "source":      "Maricopa County Recorder — Notice of Trustee Sale",
        "date_range":  {"start": start_date, "end": end_date},
        "total":       len(enriched),
        "with_address": sum(1 for r in enriched if r.get("prop_address")),
        "records":     enriched,
    }
    for path in OUTPUT_PATHS:
        save_json(output, path)
        log.info(f"Saved → {path}")

    export_ghl_csv(enriched, GHL_CSV_PATH)
    log.info(
        f"Done | total={output['total']} | with_addr={output['with_address']} "
        f"| top_score={enriched[0].get('score', 0) if enriched else 0}"
    )


# ── Run modes ─────────────────────────────────────────────────────────────────

async def run_scrape_only(start_date: str, end_date: str) -> None:
    """
    SCRAPE_ONLY mode: hit the clerk, save raw records to disk, stop.
    Does NOT run OCR or enrichment — PNGs haven't been downloaded yet.
    """
    log.info(f"[SCRAPE_ONLY] {start_date} → {end_date}")
    scraper     = ClerkScraper(lead_types=LEAD_TYPES, start_date=start_date, end_date=end_date)
    raw_records = await scraper.run()
    log.info(f"Clerk scrape: {len(raw_records)} raw records")

    # Save raw snapshot for the enrich step to pick up
    RAW_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(RAW_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(
            {
                "fetched_at": datetime.utcnow().isoformat() + "Z",
                "date_range": {"start": start_date, "end": end_date},
                "records":    raw_records,
            },
            f, indent=2, default=str,
        )
    log.info(f"Raw records saved → {RAW_JSON_PATH}")


async def run_enrich_only() -> None:
    """
    ENRICH_ONLY mode: load the raw snapshot written by run_scrape_only(),
    OCR / enrich every record (PNGs must already exist), then export.
    """
    log.info("[ENRICH_ONLY] loading raw records from disk")
    if not RAW_JSON_PATH.exists():
        log.error(
            f"{RAW_JSON_PATH} not found — run scrape step first "
            "(SCRAPE_ONLY=1) or use the combined mode."
        )
        sys.exit(1)

    with open(RAW_JSON_PATH, encoding="utf-8") as f:
        snapshot = json.load(f)

    raw_records = snapshot.get("records", [])
    date_range  = snapshot.get("date_range", {})
    start_date  = date_range.get("start", "")
    end_date    = date_range.get("end",   "")

    log.info(f"Loaded {len(raw_records)} raw records for enrichment")
    enriched = await enrich_records(raw_records)
    log.info(f"Enrichment complete: {len(enriched)} records")
    finalise(enriched, start_date, end_date)


async def run_combined(start_date: str, end_date: str) -> None:
    """
    Original single-pass mode (scrape + enrich).
    Used for local dev / manual runs where PNGs are not pre-downloaded.
    """
    log.info(f"[COMBINED] {start_date} → {end_date}")
    scraper     = ClerkScraper(lead_types=LEAD_TYPES, start_date=start_date, end_date=end_date)
    raw_records = await scraper.run()
    log.info(f"Clerk scrape: {len(raw_records)} raw records")

    enriched = await enrich_records(raw_records)
    log.info(f"Enrichment complete: {len(enriched)} records")
    finalise(enriched, start_date, end_date)


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    scrape_only  = os.getenv("SCRAPE_ONLY",  "").strip() == "1"
    enrich_only  = os.getenv("ENRICH_ONLY",  "").strip() == "1"

    if scrape_only and enrich_only:
        log.error("Cannot set both SCRAPE_ONLY=1 and ENRICH_ONLY=1")
        sys.exit(1)

    if enrich_only:
        await run_enrich_only()
    else:
        start_date, end_date = get_date_range()
        if scrape_only:
            await run_scrape_only(start_date, end_date)
        else:
            await run_combined(start_date, end_date)


if __name__ == "__main__":
    asyncio.run(main())
