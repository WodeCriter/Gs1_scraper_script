from __future__ import annotations

import json
import unittest

from gs1_scraper.models import (
    product_types_from_text,
    raw_data_for_types,
    types_from_raw_data,
)


class ProductTypeTests(unittest.TestCase):
    def test_gpc_and_product_text_produce_stable_multiple_types(self) -> None:
        product_types = product_types_from_text(
            "יין מבעבע",
            "קוקטייל יין עם ליקר",
        )

        self.assertEqual(
            product_types,
            ("wine", "liqueur", "sparkling_wine", "cocktail"),
        )

    def test_raw_data_is_json_and_discards_unknown_types(self) -> None:
        raw_data = raw_data_for_types(("gin", "wine", "not-a-type", "wine"))

        self.assertEqual(json.loads(raw_data), {"type": ["wine", "gin"]})
        self.assertEqual(types_from_raw_data(raw_data), ("wine", "gin"))
        self.assertEqual(types_from_raw_data("not json"), ())
