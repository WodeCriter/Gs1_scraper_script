"""Configuration loading and validation for the scraper."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


LOGIN_URL = "https://retailer.gs1ildigital.org/web/#/core/login/"
DASHBOARD_URL = "https://retailer.gs1ildigital.org/web/#/app/dashboard"
INCOMING_PRODUCTS_URL = (
    "https://retailer.gs1ildigital.org/web/#/app/task/incoming-products"
)
RETAILER_ID = "b1da0e73-0ce7-4556-a372-2ced574f2161"
CATEGORY = "2"
DEFAULT_OUTPUT = Path("output/gs1_alcohol_products.csv")
DEFAULT_KEYWORDS_FILE = Path("keywords.txt")


class ConfigurationError(ValueError):
    """Raised when a required scraper setting is absent or invalid."""


@dataclass(frozen=True)
class Credentials:
    email: str
    phone: str


@dataclass(frozen=True)
class ScraperSettings:
    credentials: Credentials
    output_path: Path
    keywords_file: Path
    max_products: int | None
    login_timeout_ms: int = 15 * 60 * 1_000


def load_env_file(path: Path) -> None:
    """Load a small dotenv-style file without overriding actual environment values."""

    if not path.exists():
        return

    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigurationError(f"Invalid line {line_number} in {path}: expected KEY=VALUE")

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise ConfigurationError(f"Invalid line {line_number} in {path}: empty variable name")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def load_credentials() -> Credentials:
    email = os.environ.get("GS1_EMAIL", "").strip()
    phone = os.environ.get("GS1_PHONE", "").strip()
    missing = [
        name
        for name, value in (("GS1_EMAIL", email), ("GS1_PHONE", phone))
        if not value
    ]
    if missing:
        raise ConfigurationError(
            "Missing " + ", ".join(missing) + ". Add them to .env or your environment."
        )
    return Credentials(email=email, phone=phone)


def load_keywords(path: Path) -> list[str]:
    if not path.exists():
        raise ConfigurationError(f"Keyword file does not exist: {path}")

    seen: set[str] = set()
    keywords: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        keyword = raw_line.strip()
        if not keyword or keyword.startswith("#"):
            continue
        if keyword not in seen:
            seen.add(keyword)
            keywords.append(keyword)

    if not keywords:
        raise ConfigurationError(f"Keyword file contains no usable keywords: {path}")
    return keywords


def build_settings(
    output_path: Path,
    keywords_file: Path,
    max_products: int | None,
    env_file: Path,
) -> ScraperSettings:
    load_env_file(env_file)
    if max_products is not None and max_products <= 0:
        raise ConfigurationError("--max-products must be greater than zero")
    return ScraperSettings(
        credentials=load_credentials(),
        output_path=output_path,
        keywords_file=keywords_file,
        max_products=max_products,
    )
