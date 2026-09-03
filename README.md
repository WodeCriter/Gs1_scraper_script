# GS1 Alcohol Product Scraper

This headed Playwright script signs in to the GS1 retailer portal, waits for you to press Submit and complete OTP verification, then exports unique products found through the alcohol keyword searches. For each search it selects `הכל` in the incoming-products table and follows the product route already stored on each eye button, so horizontally hidden action buttons are never clicked.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env
```

Add the email and phone values to `.env`. They are filled into the login form, but the script never clicks Submit or enters a verification code.

## Run

```bash
python main.py --fresh --max-products 1
python main.py
```

The first command is a safe manual smoke test. The normal command writes `output/gs1_alcohol_products.csv`, resumes from its checkpoint, and skips duplicate barcodes across all keywords. It never toggles the table's bulk-selection checkbox or performs accept/reject actions.

Use a new output file when needed:

```bash
python main.py --output output/my_export.csv
```

Pass `--fresh` only when you intentionally want to delete the selected CSV, checkpoint, and issue log before running. Products with missing descriptions are retained with blank cells and recorded in the adjacent `.errors.csv` file. Products without a valid barcode are skipped and logged.
