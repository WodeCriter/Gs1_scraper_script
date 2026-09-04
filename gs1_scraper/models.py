"""Data structures, normalization, and classification for exported products."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
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
    "image",
    "rawData",
)

# Keep the output order stable when a product matches more than one alcohol type.
TYPE_ORDER = (
    "wine",
    "beer",
    "vodka",
    "whisky",
    "gin",
    "rum",
    "tequila",
    "liqueur",
    "arak",
    "brandy",
    "cognac",
    "champagne",
    "sparkling_wine",
    "cider",
    "vermouth",
    "aperitif",
    "cocktail",
    "alcohol",
)

TYPE_ALIASES: dict[str, tuple[str, ...]] = {
    "wine": ("יין", "wine", "רוזה", "rose", "rosé"),
    "beer": ("בירה", "beer", "lager", "ale", "stout", "ipa"),
    "vodka": ("וודקה", "vodka"),
    "whisky": ("וויסקי", "ויסקי", "whisky", "whiskey", "bourbon"),
    "gin": ("ג'ין", "גין", "gin"),
    "rum": ("רום", "rum"),
    "tequila": ("טקילה", "tequila"),
    "liqueur": ("ליקר", "liqueur"),
    "arak": ("ערק", "arak", "arrack"),
    "brandy": ("ברנדי", "brandy"),
    "cognac": ("קוניאק", "cognac"),
    "champagne": ("שמפניה", "champagne"),
    "sparkling_wine": ("מבעבע", "sparkling", "prosecco", "פרוסקו", "cava", "קאווה"),
    "cider": ("סיידר", "cider"),
    "vermouth": ("ורמוט", "vermouth"),
    "aperitif": ("אפריטיף", "aperitif"),
    "cocktail": ("קוקטייל", "cocktail"),
    "alcohol": ("אלכוהול", "alcohol"),
}


def clean_text(value: str | None) -> str:
    return " ".join((value or "").split())


def normalize_barcode(value: str | None) -> str:
    """Return a GTIN/EAN-like digit string while retaining leading zeroes."""

    digits = "".join(character for character in (value or "") if character.isdigit())
    if 8 <= len(digits) <= 14:
        return digits
    return ""


def normalize_product_types(values: Iterable[str]) -> tuple[str, ...]:
    """Validate and order type values before persisting them."""

    known_types = {value for value in values if value in TYPE_ORDER}
    return tuple(product_type for product_type in TYPE_ORDER if product_type in known_types)


def product_types_from_text(*values: str | None) -> tuple[str, ...]:
    """Classify product text using GS1's Hebrew terms and common English terms."""

    searchable_text = clean_text(" ".join(value or "" for value in values)).lower()
    if not searchable_text:
        return ()
    return tuple(
        product_type
        for product_type in TYPE_ORDER
        if any(alias in searchable_text for alias in TYPE_ALIASES[product_type])
    )


def raw_data_for_types(product_types: Iterable[str]) -> str:
    """Serialize the CSV cell as JSON suitable for importing into a JSONB column."""

    return json.dumps(
        {"type": list(normalize_product_types(product_types))},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def types_from_raw_data(raw_data: str | None) -> tuple[str, ...]:
    """Read a prior rawData cell defensively when merging duplicate products."""

    try:
        payload = json.loads(raw_data or "{}")
    except (TypeError, json.JSONDecodeError):
        return ()
    if not isinstance(payload, dict):
        return ()

    value = payload.get("type", [])
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = [item for item in value if isinstance(item, str)]
    else:
        values = []
    return normalize_product_types(values)


def with_barcode_fallback(
    product: "ExtractedProduct",
    barcode_hint: str | None,
) -> "ExtractedProduct":
    """Use the source-table GTIN only when the detail iframe has no usable barcode."""

    if normalize_barcode(product.barcode) or not (hint := normalize_barcode(barcode_hint)):
        return product
    return replace(product, barcode=hint)


def with_text_fallback(
    product: "ExtractedProduct",
    source_description: str | None,
) -> "ExtractedProduct":
    """Fill missing iframe text from the incoming-products table description."""

    fallback = clean_text(source_description)
    if not fallback:
        return product
    return replace(
        product,
        name=clean_text(product.name) or fallback,
        description=clean_text(product.description) or fallback,
        short_description=clean_text(product.short_description) or fallback,
    )


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
    image: str
    raw_data: str

    @classmethod
    def from_extracted(
        cls,
        product: ExtractedProduct,
        *,
        image: str = "",
        product_types: Iterable[str] = (),
    ) -> "ProductRecord":
        barcode = normalize_barcode(product.barcode)
        return cls(
            retailer_id=RETAILER_ID,
            external_id=barcode,
            barcode=barcode,
            name=clean_text(product.name),
            description=clean_text(product.description),
            short_description=clean_text(product.short_description),
            category=CATEGORY,
            image=clean_text(image),
            raw_data=raw_data_for_types(product_types),
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
            "image": self.image,
            "rawData": self.raw_data,
        }


def row_signature(text: str, position: int) -> str:
    """Create a stable-enough signature for one visible result-table row."""

    normalized = re.sub(r"\s+", " ", text).strip()
    return f"{position}:{normalized}"
