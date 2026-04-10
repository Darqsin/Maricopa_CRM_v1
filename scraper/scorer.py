"""
scraper/scorer.py
Assigns a 0-100 priority score to each lead record.
"""
from datetime import datetime


def score_record(rec: dict) -> int:
    score = 0

    type_scores = {
        "NS": 40, "FL": 25, "SL": 20, "DE": 35, "PD": 15, "PJ": 15,
    }
    score += type_scores.get(rec.get("lead_key", ""), 10)

    if rec.get("prop_address"):
        score += 10
    if rec.get("mail_address"):
        score += 5

    if rec.get("prop_address") and rec.get("mail_address"):
        if rec["prop_address"].upper() != rec["mail_address"].upper():
            score += 10

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

    if rec.get("parcel"):
        score += 3
    if rec.get("trustee_name"):
        score += 2
    if rec.get("pdf_url"):
        score += 2

    return min(score, 100)
