"""
scraper/scorer.py
Assigns a 0–100 priority score to each lead record.
Higher score = higher motivated-seller probability.
"""

from datetime import datetime


def score_record(rec: dict) -> int:
    score = 0

    # ── 1. Lead type weights ───────────────────────────────────────────────
    type_scores = {
        "NS": 40,   # Notice of Trustee Sale — strongest signal
        "FL": 25,   # Federal Tax Lien
        "SL": 20,   # State Tax Lien
        "DE": 35,   # Tax Deed
        "PD": 15,   # Probate
        "PJ": 15,
    }
    score += type_scores.get(rec.get("lead_key", ""), 10)

    # ── 2. Address completeness ────────────────────────────────────────────
    if rec.get("prop_address"):
        score += 10
    if rec.get("mail_address"):
        score += 5

    # ── 3. Absentee owner ─────────────────────────────────────────────────
    if rec.get("prop_address") and rec.get("mail_address"):
        if rec["prop_address"].upper() != rec["mail_address"].upper():
            score += 10

    # ── 4. Auction urgency ─────────────────────────────────────────────────
    if rec.get("auction_date"):
        try:
            auc  = datetime.strptime(rec["auction_date"], "%Y-%m-%d").date()
            days = (auc - datetime.utcnow().date()).days
            if days <= 14:
                score += 20
            elif days <= 30:
                score += 12
            elif days <= 60:
                score += 6
        except Exception:
            pass

    # ── 5. Loan value ──────────────────────────────────────────────────────
    amount = rec.get("amount")
    if amount:
        try:
            a = float(amount)
            if a >= 1_000_000:
                score += 8
            elif a >= 500_000:
                score += 5
            elif a >= 200_000:
                score += 3
        except (ValueError, TypeError):
            pass

    # ── 6. Parcel available ────────────────────────────────────────────────
    if rec.get("parcel"):
        score += 3

    # ── 7. Trustee info (NTS only) ─────────────────────────────────────────
    if rec.get("trustee_name"):
        score += 2
    if rec.get("pdf_url"):
        score += 2

    return min(score, 100)
