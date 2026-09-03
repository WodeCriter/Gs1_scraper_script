"""Manual, headed authentication helpers for the GS1 portal."""

from __future__ import annotations

import re

from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError

from .config import Credentials, DASHBOARD_URL, LOGIN_URL


EMAIL_SELECTORS = (
    "input[type='email']",
    "input[name*='email' i]",
    "input[id*='email' i]",
    "input[placeholder*='מייל']",
    "input[placeholder*='דוא']",
)
PHONE_SELECTORS = (
    "input[type='tel']",
    "input[name*='phone' i]",
    "input[id*='phone' i]",
    "input[name*='mobile' i]",
    "input[id*='mobile' i]",
    "input[placeholder*='טלפון']",
    "input[placeholder*='נייד']",
)


class LoginError(RuntimeError):
    """Raised when the portal login form cannot be prepared safely."""


def _first_visible(page: Page, selectors: tuple[str, ...], timeout_ms: int) -> Locator:
    # The overall timeout is for the human OTP step, not selector discovery.
    timeout_per_selector = min(5_000, max(1_000, timeout_ms // max(1, len(selectors))))
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            locator.wait_for(state="visible", timeout=timeout_per_selector)
            return locator
        except PlaywrightTimeoutError:
            continue
    raise LoginError(
        "Could not locate a login field. The portal may have changed its form markup."
    )


def prepare_manual_login(
    page: Page,
    credentials: Credentials,
    timeout_ms: int,
) -> None:
    """Fill the first login form and wait for the user to complete Submit and OTP."""

    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    email_field = _first_visible(page, EMAIL_SELECTORS, timeout_ms)
    phone_field = _first_visible(page, PHONE_SELECTORS, timeout_ms)
    email_field.fill(credentials.email)
    phone_field.fill(credentials.phone)

    print(
        "Browser ready: press Submit in the GS1 window, then enter the verification code. "
        "The scraper will continue after the dashboard opens."
    )
    try:
        page.wait_for_url(
            re.compile(r".*/web/#/app/dashboard(?:[/?].*)?$"), timeout=timeout_ms
        )
    except PlaywrightTimeoutError as error:
        raise LoginError(
            "Timed out waiting for manual login. No Submit or OTP action was automated."
        ) from error

    if not page.url.startswith(DASHBOARD_URL):
        raise LoginError(f"Unexpected URL after login: {page.url}")
