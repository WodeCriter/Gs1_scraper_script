from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from gs1_scraper.models import ExtractedProduct, ProductRecord
from gs1_scraper.storage import ProductStore, checkpoint_path_for, error_path_for


class ProductStoreTests(unittest.TestCase):
    def _record(self) -> ProductRecord:
        return ProductRecord.from_extracted(
            ExtractedProduct(
                barcode="7290018359815",
                name="יין",
                description="תיאור",
                short_description="קצר",
            )
        )

    def test_csv_is_bom_encoded_and_duplicate_barcodes_are_not_written_twice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "products.csv"
            store = ProductStore(output_path)
            self.assertTrue(store.write(self._record()).written)
            duplicate = store.write(self._record())
            self.assertFalse(duplicate.written)
            self.assertEqual(duplicate.reason, "duplicate_barcode")
            self.assertTrue(output_path.read_bytes().startswith(b"\xef\xbb\xbf"))

            with output_path.open("r", encoding="utf-8-sig", newline="") as file_handle:
                rows = list(csv.DictReader(file_handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["externalId"], rows[0]["barcode"])

    def test_checkpoint_survives_a_new_store_instance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "products.csv"
            store = ProductStore(output_path)
            store.write(self._record())
            store.mark_keyword_complete("יין")

            resumed_store = ProductStore(output_path)
            self.assertIn("7290018359815", resumed_store.seen_barcodes)
            self.assertIn("יין", resumed_store.completed_keywords)

    def test_reset_removes_only_export_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "products.csv"
            store = ProductStore(output_path)
            store.write(self._record())
            store.mark_keyword_complete("יין")
            error_path_for(output_path).write_text("log", encoding="utf-8")

            ProductStore.reset(output_path)
            self.assertFalse(output_path.exists())
            self.assertFalse(checkpoint_path_for(output_path).exists())
            self.assertFalse(error_path_for(output_path).exists())
