"""Data structures and normalization rules for exported products."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from .config import CATEGORY, RETAILER_ID


CSV_COLUMNS = (
    "retailerId",
    "externalId",
    "barcode",
    "name",
    "description",
    "short_description",
    "category",
)


def clean_text(value: str | None) -> str:
    return " ".join((value or "").split())


def normalize_barcode(value: str | None) -> str:
    """Return a GTIN/EAN-like digit string while retaining leading zeroes."""

    digits = "".join(character for character in (value or "") if character.isdigit())
    if 8 <= len(digits) <= 14:
        return digits
    return ""


def with_barcode_fallback(
    product: "ExtractedProduct",
    barcode_hint: str | None,
) -> "ExtractedProduct":
    """Use the source-table GTIN only when the detail iframe has no usable barcode."""

    if normalize_barcode(product.barcode) or not (hint := normalize_barcode(barcode_hint)):
        return product
    return replace(product, barcode=hint)


@dataclass(frozen=True)
class ExtractedProduct:
    barcode: str = ""
    name: str = ""
    description: str = ""
    short_description: str = ""

    @property
    def missing_text_fields(self) -> tuple[str, ...]:
        fields = {
            "name": self.name,
            "description": self.description,
            "short_description": self.short_description,
        }
        return tuple(field for field, value in fields.items() if not clean_text(value))


@dataclass(frozen=True)
class ProductRecord:
    retailer_id: str
    external_id: str
    barcode: str
    name: str
    description: str
    short_description: str
    category: str

    @classmethod
    def from_extracted(cls, product: ExtractedProduct) -> "ProductRecord":
        barcode = normalize_barcode(product.barcode)
        return cls(
            retailer_id=RETAILER_ID,
            external_id=barcode,
            barcode=barcode,
            name=clean_text(product.name),
            description=clean_text(product.description),
            short_description=clean_text(product.short_description),
            category=CATEGORY,
        )

    def to_csv_row(self) -> dict[str, str]:
        return {
            "retailerId": self.retailer_id,
            "externalId": self.external_id,
            "barcode": self.barcode,
            "name": self.name,
            "description": self.description,
            "short_description": self.short_description,
            "category": self.category,
        }


def row_signature(text: str, position: int) -> str:
    """Create a stable-enough signature for one visible result-table row."""

    normalized = re.sub(r"\s+", " ", text).strip()
    return f"{position}:{normalized}"
