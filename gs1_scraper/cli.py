"""Command-line entry point for the GS1 alcohol product scraper."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import (
    DEFAULT_KEYWORDS_FILE,
    DEFAULT_OUTPUT,
    ConfigurationError,
    build_settings,
    load_keywords,
)
from .scraper import Gs1AlcoholScraper
from .storage import ErrorLogger, ProductStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export unique alcohol products from the GS1 retailer portal."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"CSV destination (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--keywords-file",
        type=Path,
        default=DEFAULT_KEYWORDS_FILE,
        help=f"One search term per line (default: {DEFAULT_KEYWORDS_FILE})",
    )
    parser.add_argument(
        "--max-products",
        type=int,
        help="Stop after this many detail-page attempts; useful for a manual smoke test.",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Delete the selected CSV, checkpoint, and issue log before starting.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="dotenv-style credentials file (default: .env)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings = build_settings(
            output_path=args.output,
            keywords_file=args.keywords_file,
            max_products=args.max_products,
            env_file=args.env_file,
        )
        keywords = load_keywords(settings.keywords_file)
        if args.fresh:
            ProductStore.reset(settings.output_path)

        store = ProductStore(settings.output_path)
        scraper = Gs1AlcoholScraper(
            settings=settings,
            store=store,
            error_logger=ErrorLogger(settings.output_path),
        )
        summary = scraper.run(keywords)
    except ConfigurationError as error:
        parser.error(str(error))
    except KeyboardInterrupt:
        print("Stopped. The CSV and checkpoint retain completed work.", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"Scraper failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    print(
        "Finished: "
        f"attempted={summary.attempted}, exported={summary.exported}, "
        f"duplicates={summary.duplicates}, missing_barcodes={summary.missing_barcodes}, "
        f"issues={summary.issues}, completed_keywords={summary.completed_keywords}."
    )
    return 0
