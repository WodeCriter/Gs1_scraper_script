from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from gs1_scraper.config import Credentials, ScraperSettings
from gs1_scraper.navigation import KeywordResult, NavigationError
from gs1_scraper.scraper import Gs1AlcoholScraper
from gs1_scraper.storage import ErrorLogger, ProductStore, error_path_for


class ScraperRecoveryTests(unittest.TestCase):
    def test_unavailable_keyword_is_logged_and_later_keywords_continue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "products.csv"
            settings = ScraperSettings(
                credentials=Credentials(email="user@example.test", phone="0500000000"),
                output_path=output_path,
                keywords_file=Path(directory) / "keywords.txt",
                max_products=None,
            )
            store = ProductStore(output_path)
            scraper = Gs1AlcoholScraper(settings, store, ErrorLogger(output_path))

            with (
                patch("gs1_scraper.scraper.sync_playwright") as mock_sync_playwright,
                patch("gs1_scraper.scraper.prepare_manual_login"),
                patch("gs1_scraper.scraper.IncomingProductsNavigator") as mock_navigator,
            ):
                playwright = MagicMock()
                browser = MagicMock()
                context = MagicMock()
                page = MagicMock()
                mock_sync_playwright.return_value.__enter__.return_value = playwright
                playwright.chromium.launch.return_value = browser
                browser.new_context.return_value = context
                context.new_page.return_value = page

                navigator = mock_navigator.return_value
                navigator.process_keyword.side_effect = [
                    NavigationError("Incoming products table did not become available."),
                    KeywordResult(completed=True, discovered=0),
                ]

                summary = scraper.run(["יין", "בירה"])

            self.assertEqual(navigator.process_keyword.call_count, 2)
            self.assertEqual(summary.completed_keywords, 1)
            self.assertNotIn("יין", store.completed_keywords)
            self.assertIn("בירה", store.completed_keywords)

            with error_path_for(output_path).open(
                "r", encoding="utf-8-sig", newline=""
            ) as file_handle:
                rows = list(csv.DictReader(file_handle))
            self.assertEqual(rows[0]["event"], "keyword_navigation_failure")
            self.assertEqual(rows[0]["keyword"], "יין")
