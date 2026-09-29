"""Command-line entry point for the GS1 keyword and product scraper."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

from .config import (
    DEFAULT_KEYWORDS_FILE,
    DEFAULT_OUTPUT,
    DEFAULT_PRODUCTS_FILE,
    DEFAULT_PRODUCT_OUTPUT,
    ConfigurationError,
    build_settings,
    load_keywords,
)
from .models import ExportProfile
from .scraper import Gs1Scraper
from .searches import (
    SearchKind,
    keyword_search_tasks,
    load_product_searches,
)
from .storage import ErrorLogger, ProductStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export products from the GS1 retailer portal."
    )
    parser.add_argument(
        "--output",
        type=Path,
        help=(
            "CSV destination (defaults: "
            f"{DEFAULT_OUTPUT} for keywords, {DEFAULT_PRODUCT_OUTPUT} for products)"
        ),
    )
    parser.add_argument(
        "--keywords-file",
        type=Path,
        default=DEFAULT_KEYWORDS_FILE,
        help=f"One search term per line (default: {DEFAULT_KEYWORDS_FILE})",
    )
    parser.add_argument(
        "--products-file",
        type=Path,
        default=DEFAULT_PRODUCTS_FILE,
        help=f"Product workbook (default: {DEFAULT_PRODUCTS_FILE})",
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


def prompt_search_kind(
    input_fn: Callable[[str], str] | None = None,
    output_fn: Callable[[str], None] = print,
) -> SearchKind:
    """Prompt until the user selects one of the two supported workflows."""

    read = input_fn or input
    output_fn("Choose a search mode:")
    output_fn("1) Keyword search (all matching products)")
    output_fn("2) Product-name search from workbook (first result only)")
    while True:
        try:
            choice = read("Enter 1 or 2: ").strip()
        except EOFError as error:
            raise ConfigurationError("No menu selection was provided.") from error
        if choice == "1":
            return SearchKind.KEYWORD
        if choice == "2":
            return SearchKind.PRODUCT
        output_fn("Invalid selection. Enter 1 or 2.")


def default_output_for(search_kind: SearchKind) -> Path:
    if search_kind is SearchKind.PRODUCT:
        return DEFAULT_PRODUCT_OUTPUT
    return DEFAULT_OUTPUT


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        search_kind = prompt_search_kind()
        settings = build_settings(
            output_path=args.output or default_output_for(search_kind),
            keywords_file=args.keywords_file,
            products_file=args.products_file,
            max_products=args.max_products,
            env_file=args.env_file,
        )
        if search_kind is SearchKind.KEYWORD:
            searches = keyword_search_tasks(load_keywords(settings.keywords_file))
            export_profile = ExportProfile.KEYWORD
        else:
            searches = load_product_searches(settings.products_file)
            export_profile = ExportProfile.PRODUCT
        if args.fresh:
            ProductStore.reset(settings.output_path)

        store = ProductStore(settings.output_path, export_profile=export_profile)
        scraper = Gs1Scraper(
            settings=settings,
            store=store,
            error_logger=ErrorLogger(settings.output_path),
        )
        summary = scraper.run(searches)
    except ConfigurationError as error:
        parser.error(str(error))
    except KeyboardInterrupt:
        print("Stopped. The CSV and checkpoint retain completed work.", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"Scraper failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    completed_label = (
        "completed_products"
        if search_kind is SearchKind.PRODUCT
        else "completed_keywords"
    )
    print(
        "Finished: "
        f"attempted={summary.attempted}, exported={summary.exported}, "
        f"duplicates={summary.duplicates}, missing_barcodes={summary.missing_barcodes}, "
        f"issues={summary.issues}, {completed_label}={summary.completed_searches}."
    )
    return 0
