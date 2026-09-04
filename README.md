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

The first command is a safe manual smoke test. The normal command writes `output/gs1_alcohol_products.csv`, resumes from its checkpoint, and keeps one row per barcode across all keywords. It never toggles the table's bulk-selection checkbox or performs accept/reject actions.

Use a new output file when needed:

```bash
python main.py --output output/my_export.csv
```

The export columns are `retailerId`, `externalId`, `barcode`, `name`, `description`, `short_description`, `category`, `image`, and `rawData`. `image` contains the GS1 image URL. `rawData` is JSON such as `{"type":["wine","liqueur"]}` and merges types when a product appears through more than one keyword.

The incoming table's `תיאור מוצר` is used when the detail iframe does not expose name or description fields. A normal zero-result keyword is marked complete and the scraper proceeds to the next one. If the whole table fails to load, that keyword is logged and left incomplete so a later run can retry it.

Pass `--fresh` only when you intentionally want to delete the selected CSV, checkpoint, and issue log before running. The enriched schema is not compatible with the prior seven-column CSV, so rebuild the existing export with `--fresh` or choose a new `--output` path. Products without a valid barcode are skipped and logged.
