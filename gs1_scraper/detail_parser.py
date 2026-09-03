"""Extract product fields from the GS1 product-editor iframe."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from playwright.sync_api import FrameLocator, TimeoutError as PlaywrightTimeoutError

from .models import ExtractedProduct, clean_text


LABEL_ALIASES: dict[str, tuple[str, ...]] = {
    "barcode": ("מספר ברקוד", "ברקוד", "gtin", "ean", "קוד מוצר"),
    "name": ("שם המוצר", "שם מוצר", "שם"),
    "description": ("תיאור מלא", "תיאור המוצר", "תיאור מוצר", "תיאור"),
    "short_description": ("תיאור קצר", "תיאור מקוצר", "תיאור תמציתי"),
}

EXCLUDED_LABEL_WORDS: dict[str, tuple[str, ...]] = {
    "description": ("קצר", "מקוצר", "תמציתי"),
    "short_description": ("מלא",),
}


DETAIL_FIELD_SCRIPT = r"""
(body) => {
  const clean = (value) => (value || "").replace(/\s+/g, " ").trim();
  const pairs = [];
  const seen = new Set();
  const add = (label, value) => {
    label = clean(label);
    value = clean(value);
    const key = `${label}\u0000${value}`;
    if (label && value && !seen.has(key)) {
      seen.add(key);
      pairs.push({label, value});
    }
  };
  const controlValue = (element) => {
    if (!element) return "";
    if (element.matches("input, textarea, select")) {
      if (element.type === "checkbox" || element.type === "radio") {
        return element.checked ? "true" : "false";
      }
      return element.value;
    }
    return element.innerText || element.textContent || "";
  };
  const firstValue = (root) => {
    const control = root.querySelector(
      "input, textarea, select, [contenteditable='true'], .form-control-static, .value, .field-value"
    );
    return controlValue(control);
  };

  body.querySelectorAll("label").forEach((label) => {
    const byFor = label.htmlFor ? body.querySelector(`#${CSS.escape(label.htmlFor)}`) : null;
    const nested = label.querySelector("input, textarea, select");
    add(label.innerText || label.textContent, controlValue(byFor || nested));
  });

  body.querySelectorAll("tr").forEach((row) => {
    const cells = row.querySelectorAll(":scope > th, :scope > td");
    if (cells.length >= 2) {
      const values = Array.from(cells).slice(1).map(controlValue).filter(Boolean).join(" ");
      add(controlValue(cells[0]), values);
    }
  });

  body.querySelectorAll("dt").forEach((term) => {
    add(controlValue(term), controlValue(term.nextElementSibling));
  });

  body.querySelectorAll(".form-group, .field, .form-row, .control-group").forEach((group) => {
    const label = group.querySelector("label, .control-label, .field-label, dt, th");
    add(controlValue(label), firstValue(group));
  });

  return pairs;
}
"""


def normalize_label(value: str) -> str:
    normalized = clean_text(value).lower()
    normalized = re.sub(r"[:_\-()\[\]{}]", " ", normalized)
    return clean_text(normalized)


def _candidate_score(label: str, alias: str) -> int:
    if label == alias:
        return 1_000 + len(alias)
    if label.startswith(alias):
        return 700 + len(alias)
    if alias in label:
        return 400 + len(alias)
    return 0


def _resolve_field(
    pairs: Iterable[tuple[str, str]],
    field: str,
) -> str:
    aliases = tuple(normalize_label(alias) for alias in LABEL_ALIASES[field])
    excluded_words = tuple(
        normalize_label(word) for word in EXCLUDED_LABEL_WORDS.get(field, ())
    )
    best_score = 0
    best_value = ""

    for raw_label, raw_value in pairs:
        label = normalize_label(raw_label)
        value = clean_text(raw_value)
        if not label or not value or any(word in label for word in excluded_words):
            continue
        score = max((_candidate_score(label, alias) for alias in aliases), default=0)
        if score > best_score:
            best_score = score
            best_value = value
    return best_value


def parse_label_value_pairs(
    pairs: Mapping[str, str] | Iterable[tuple[str, str]],
) -> ExtractedProduct:
    """Map arbitrary labeled GS1 fields to the export schema."""

    items = list(pairs.items()) if isinstance(pairs, Mapping) else list(pairs)
    return ExtractedProduct(
        barcode=_resolve_field(items, "barcode"),
        name=_resolve_field(items, "name"),
        description=_resolve_field(items, "description"),
        short_description=_resolve_field(items, "short_description"),
    )


@dataclass
class ProductDetailParser:
    timeout_ms: int = 20_000

    def extract_from_frame(self, frame: FrameLocator) -> ExtractedProduct:
        body = frame.locator("body")
        body.wait_for(state="attached", timeout=self.timeout_ms)
        try:
            body.locator("input, textarea, table, dl, label").first.wait_for(
                state="attached", timeout=self.timeout_ms
            )
        except PlaywrightTimeoutError:
            # Some product-editor variants expose their values as plain text only.
            pass

        raw_pairs = body.evaluate(DETAIL_FIELD_SCRIPT)
        pairs = [
            (str(item.get("label", "")), str(item.get("value", "")))
            for item in raw_pairs
            if isinstance(item, dict)
        ]
        return parse_label_value_pairs(pairs)
