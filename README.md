[README.md](https://github.com/user-attachments/files/26631653/README.md)
# Maricopa_CRM_v1
Maricopa foreclosures
# Maricopa County Motivated Seller Lead Scraper

Automated daily scraper for Maricopa County, AZ public records — surfacing motivated seller leads from Notice of Trustee Sales, Tax Liens, Tax Deeds, and Probate filings.

## 🏗️ Architecture

```
Project/
├── scraper/
│   ├── fetch.py           # Orchestrator — run this
│   ├── clerk_scraper.py   # Playwright async scraper (recorder portal)
│   ├── enricher.py        # Detail page enrichment (address, parcel, PDF, NTS fields)
│   ├── scorer.py          # Priority scoring (0–100)
│   └── exporter.py        # JSON + GHL CSV output
│
├── maricopa_nts_png_v01.py    # PNG → grouped PDF pipeline (OCR)
├── maricopa_nts_parser_v01.py # PDF → Excel/CSV field extractor
│
├── raw_png/               # Drop county-downloaded PNGs here
├── grouped_output/
│   ├── renamed/           # OCR-renamed PNGs
│   └── pdfs/              # One PDF per document number
├── parsed_output/
│   ├── nts_data.xlsx
│   └── nts_data.csv
│
├── data/
│   ├── records.json       # Full lead JSON output
│   └── ghl_export.csv     # GoHighLevel import-ready CSV
│
├── dashboard/
│   ├── index.html         # Interactive lead dashboard
│   └── records.json       # Dashboard data copy
│
├── .github/workflows/
│   └── scrape.yml         # GitHub Actions: daily 7 AM UTC + manual trigger
│
└── requirements.txt
```

## 🚀 Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
python -m playwright install --with-deps chromium
# For OCR pipeline:
# Ubuntu: sudo apt-get install tesseract-ocr
# macOS:  brew install tesseract
```

### 2. Run the scraper

```bash
python scraper/fetch.py
```

Outputs:
- `data/records.json` — full structured lead data
- `dashboard/records.json` — dashboard copy
- `data/ghl_export.csv` — GHL import CSV

### 3. View dashboard locally

```bash
cd dashboard
python -m http.server 8080
# Open http://localhost:8080
```

### 4. PNG → PDF pipeline (if you have downloaded images)

```bash
# Place PNGs in raw_png/
python maricopa_nts_png_v01.py    # OCR → grouped PDFs
python maricopa_nts_parser_v01.py # Parse PDFs → Excel/CSV
```

## 🤖 Automation (GitHub Actions)

1. Push this repo to GitHub
2. Enable **GitHub Pages** in repo Settings → Pages → Source: GitHub Actions
3. The workflow at `.github/workflows/scrape.yml` runs automatically at **7 AM UTC daily**
4. Optionally set a repo variable `LOOKBACK_DAYS` (default: 7)

The action:
- Scrapes the clerk portal
- Enriches and scores all records
- Commits `records.json` files back to the repo
- Deploys the dashboard to GitHub Pages

## 📊 Lead Types

| Key | Type | Category | Priority |
|-----|------|----------|----------|
| NS  | Notice of Trustee Sale | NOTS | ⭐⭐⭐ Highest |
| DE  | Tax Deed | TAX | ⭐⭐⭐ High |
| FL  | Federal Tax Lien | LIEN | ⭐⭐ Medium |
| SL  | State Tax Lien | LIEN | ⭐⭐ Medium |
| PD/PJ | Probate Document | PRO | ⭐ Base |

## 📋 Output Schema

Each record in `records.json`:

```json
{
  "doc_num":      "20260189838",
  "doc_type":     "NOTS",
  "filed":        "2026-04-07",
  "cat":          "NOTS",
  "cat_label":    "Notice of Trustee Sale",
  "lead_key":     "NS",
  "owner":        "SMITH JOHN W",
  "first_name":   "John",
  "last_name":    "Smith",
  "first_name_2": "",
  "last_name_2":  "",
  "amount":       387500.00,
  "prop_address": "2847 E PALM BEACH DR",
  "prop_city":    "Chandler",
  "prop_state":   "AZ",
  "prop_zip":     "85249",
  "mail_address": "8821 N 16TH ST APT 4",
  "mail_city":    "Phoenix",
  "mail_state":   "AZ",
  "mail_zip":     "85020",
  "parcel":       "304-71-042",
  "trustee_name": "Desert Trustee Services Inc",
  "trustee_phone":"(480) 555-0192",
  "auction_date": "2026-05-05",
  "pdf_url":      "https://recorder.maricopa.gov/...",
  "clerk_url":    "https://recorder.maricopa.gov/...",
  "flags":        ["TRUSTEE_SALE", "ABSENTEE_OWNER", "AUCTION_SOON"],
  "score":        87
}
```

## 🏷️ Scoring System (0–100)

| Signal | Points |
|--------|--------|
| Notice of Trustee Sale | +40 |
| Tax Deed | +35 |
| Federal Tax Lien | +25 |
| Property address found | +10 |
| Absentee owner | +10 |
| Auction ≤14 days | +20 |
| Auction ≤30 days | +12 |
| Auction ≤60 days | +6 |
| Loan > $1M | +8 |
| Parcel number found | +3 |

## 🔧 Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `LOOKBACK_DAYS` | 7 | Days of history to pull |

Set as environment variable or GitHub Actions repo variable.

## ⚠️ Notes

- The Maricopa Recorder portal uses JavaScript-heavy dynamic pages. Playwright handles this.
- Rate limiting: 400ms delay between enrichment requests (polite crawling).
- The scraper never crashes on bad individual records — errors are logged and skipped.
- Retry logic: 3 attempts per doc type, with exponential backoff.
- All dates normalized to `YYYY-MM-DD`.
