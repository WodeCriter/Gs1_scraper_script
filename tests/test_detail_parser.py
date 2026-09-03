from __future__ import annotations

import unittest

from gs1_scraper.detail_parser import normalize_label, parse_label_value_pairs
from gs1_scraper.models import (
    ExtractedProduct,
    ProductRecord,
    normalize_barcode,
    with_barcode_fallback,
)
from tests.fixtures import PRODUCT_LABEL_PAIRS


class DetailParserTests(unittest.TestCase):
    def test_label_pairs_map_to_export_fields(self) -> None:
        product = parse_label_value_pairs(PRODUCT_LABEL_PAIRS)
        self.assertEqual(product.barcode, "729 001 835 9815")
        self.assertEqual(product.name, "יין אדום לדוגמה")
        self.assertEqual(product.description, "תיאור מלא של המוצר")
        self.assertEqual(product.short_description, "תיאור קצר")

    def test_short_description_does_not_fill_full_description(self) -> None:
        product = parse_label_value_pairs(
            [
                ("ברקוד", "7290018359815"),
                ("שם מוצר", "מוצר"),
                ("תיאור קצר", "רק תיאור קצר"),
            ]
        )
        self.assertEqual(product.description, "")
        self.assertEqual(product.short_description, "רק תיאור קצר")

    def test_record_duplicates_the_normalized_barcode_as_external_id(self) -> None:
        product = parse_label_value_pairs(PRODUCT_LABEL_PAIRS)
        record = ProductRecord.from_extracted(product)
        self.assertEqual(record.barcode, "7290018359815")
        self.assertEqual(record.external_id, "7290018359815")

    def test_label_and_barcode_normalization(self) -> None:
        self.assertEqual(normalize_label(" תיאור-מוצר: "), "תיאור מוצר")
        self.assertEqual(normalize_barcode("729 001 835 9815"), "7290018359815")
        self.assertEqual(normalize_barcode("not a barcode"), "")

    def test_table_gtin_is_used_only_when_the_detail_lacks_a_barcode(self) -> None:
        fallback_product = with_barcode_fallback(
            ExtractedProduct(name="יין"), "7290018359815"
        )
        self.assertEqual(fallback_product.barcode, "7290018359815")

        detail_product = with_barcode_fallback(
            ExtractedProduct(barcode="7290018359822"), "7290018359815"
        )
        self.assertEqual(detail_product.barcode, "7290018359822")
