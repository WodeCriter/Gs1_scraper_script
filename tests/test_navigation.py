from __future__ import annotations

import unittest

from gs1_scraper.models import row_signature
from gs1_scraper.navigation import build_product_candidate, table_total
from tests.fixtures import DETAIL_HREF, TABLE_INFO_ALL, TABLE_INFO_PAGED, TABLE_ROW_TEXTS


class NavigationFixtureTests(unittest.TestCase):
    def test_row_signatures_keep_table_rows_distinct(self) -> None:
        signatures = [
            row_signature(text, position) for position, text in enumerate(TABLE_ROW_TEXTS)
        ]
        self.assertEqual(len(signatures), len(set(signatures)))
        self.assertEqual(signatures[0], "0:7290018359815 יין אדום לדוגמה")

    def test_hidden_eye_href_becomes_a_direct_product_route(self) -> None:
        candidate = build_product_candidate(
            keyword="יין",
            base_url=(
                "https://retailer.gs1ildigital.org/web/#/app/task/incoming-products"
            ),
            detail_href=DETAIL_HREF,
            gtin="7290018359815",
            row_text=TABLE_ROW_TEXTS[0],
            position=0,
        )
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.barcode_hint, "7290018359815")
        self.assertEqual(
            candidate.detail_url,
            "https://retailer.gs1ildigital.org/web/" + DETAIL_HREF,
        )

    def test_table_total_handles_localized_paged_and_all_results_text(self) -> None:
        self.assertEqual(table_total(TABLE_INFO_PAGED), 1_076)
        self.assertEqual(table_total(TABLE_INFO_ALL), 1_076)

    def test_candidate_without_an_eye_route_is_ignored(self) -> None:
        candidate = build_product_candidate(
            keyword="יין",
            base_url="https://retailer.gs1ildigital.org/web/#/app/task/incoming-products",
            detail_href="",
            gtin="7290018359815",
            row_text=TABLE_ROW_TEXTS[0],
            position=0,
        )
        self.assertIsNone(candidate)
