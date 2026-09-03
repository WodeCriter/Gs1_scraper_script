"""Direct product-route traversal for the GS1 incoming-products DataTable."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin

from playwright.sync_api import (
    Error as PlaywrightError,
    FrameLocator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

from .config import INCOMING_PRODUCTS_URL
from .models import normalize_barcode, row_signature


TABLE_SELECTOR = "#dt_incoming_products"
SEARCH_SELECTOR = "input[aria-controls='dt_incoming_products']"
LENGTH_SELECTOR = "select[name='dt_incoming_products_length']"
ROWS_SELECTOR = f"{TABLE_SELECTOR} tbody tr"
INFO_SELECTOR = "#dt_incoming_products_info"
PROCESSING_SELECTOR = "#dt_incoming_products_processing"
DETAIL_BUTTON_SELECTOR = "button[ui-sref*='app.task.product_info'][href]"
NEXT_SELECTOR = "#dt_incoming_products_next"


class NavigationError(RuntimeError):
    """Raised when the portal cannot be moved to a known product-table state."""


@dataclass(frozen=True)
class ProductCandidate:
    keyword: str
    detail_url: str
    barcode_hint: str
    signature: str


@dataclass(frozen=True)
class KeywordResult:
    completed: bool
    discovered: int


@dataclass(frozen=True)
class TableState:
    search: str
    length: str
    info: str
    body_text: str


def table_total(info_text: str) -> int | None:
    """Return the final total from DataTables' localized info text."""

    numbers = re.findall(r"\d[\d,]*", info_text)
    if not numbers:
        return None
    return int(numbers[-1].replace(",", ""))


def build_product_candidate(
    *,
    keyword: str,
    base_url: str,
    detail_href: str,
    gtin: str,
    row_text: str,
    position: int,
) -> ProductCandidate | None:
    """Build a direct-detail candidate from a table row without clicking it."""

    if not detail_href:
        return None
    return ProductCandidate(
        keyword=keyword,
        detail_url=urljoin(base_url, detail_href),
        barcode_hint=normalize_barcode(gtin),
        signature=row_signature(row_text, position),
    )


class IncomingProductsNavigator:
    def __init__(self, page: Page, timeout_ms: int = 30_000) -> None:
        self.page = page
        self.timeout_ms = timeout_ms

    def open(self) -> None:
        """Open the incoming-products route without relying on sidebar visibility."""

        if self._incoming_table_is_visible():
            return
        self.page.goto(INCOMING_PRODUCTS_URL, wait_until="domcontentloaded")
        self._wait_for_table()

    def process_keyword(
        self,
        keyword: str,
        on_product: Callable[[FrameLocator, ProductCandidate], None],
        on_failure: Callable[[ProductCandidate, Exception], None],
        on_attempt: Callable[[ProductCandidate], None],
        should_stop: Callable[[], bool],
    ) -> KeywordResult:
        """Snapshot matching detail routes, then visit each route once."""

        self.open()
        self.apply_search(keyword)
        candidates = self._collect_candidates(keyword)
        print(f"Found {len(candidates)} candidate products for: {keyword}")

        for candidate in candidates:
            if should_stop():
                return KeywordResult(completed=False, discovered=len(candidates))

            on_attempt(candidate)
            try:
                frame = self._open_candidate(candidate)
                on_product(frame, candidate)
            except Exception as error:  # One malformed product must not halt a long export.
                on_failure(candidate, error)

        return KeywordResult(completed=True, discovered=len(candidates))

    def apply_search(self, keyword: str) -> None:
        self._wait_for_table()
        previous_state = self._read_table_state()
        self.page.locator(SEARCH_SELECTOR).first.fill(keyword)
        self._wait_for_table_update(
            expected_search=keyword,
            expected_length=previous_state.length,
            previous_state=previous_state,
            allow_unchanged=previous_state.search == keyword,
        )

    def show_all_results(self) -> bool:
        """Choose DataTables' all-results option and confirm every matching row rendered."""

        self._wait_for_table()
        length_select = self.page.locator(LENGTH_SELECTOR).first
        if length_select.input_value() != "-1":
            previous_state = self._read_table_state()
            length_select.select_option("-1")
            total_before = table_total(previous_state.info)
            self._wait_for_table_update(
                expected_search=previous_state.search,
                expected_length="-1",
                previous_state=previous_state,
                # Fewer than 16 matches may render identically after the selection.
                allow_unchanged=total_before is not None and total_before <= 15,
            )

        total = table_total(self._read_table_state().info)
        return total is not None and self._current_product_row_count() >= total

    def _collect_candidates(self, keyword: str) -> list[ProductCandidate]:
        all_rows_rendered = self.show_all_results()
        candidates = self._snapshot_current_page_candidates(keyword)
        if all_rows_rendered:
            return candidates

        print("GS1 did not render every result for 'הכל'; using the visible paginator.")
        expected_total = table_total(self._read_table_state().info)
        seen_row_count = self._current_product_row_count()
        seen_urls = {candidate.detail_url for candidate in candidates}
        while self._go_to_next_page():
            seen_row_count += self._current_product_row_count()
            for candidate in self._snapshot_current_page_candidates(keyword):
                if candidate.detail_url not in seen_urls:
                    seen_urls.add(candidate.detail_url)
                    candidates.append(candidate)
        if expected_total is not None and seen_row_count < expected_total:
            raise NavigationError(
                "GS1 returned fewer product rows than its table total; refusing an incomplete export."
            )
        return candidates

    def _snapshot_current_page_candidates(self, keyword: str) -> list[ProductCandidate]:
        rows = self.page.locator(ROWS_SELECTOR)
        candidates: list[ProductCandidate] = []
        seen_urls: set[str] = set()
        for position in range(rows.count()):
            row = rows.nth(position)
            button = row.locator(DETAIL_BUTTON_SELECTOR).first
            detail_href = button.get_attribute("href") if button.count() else None
            cells = row.locator("td")
            gtin = cells.nth(1).inner_text() if cells.count() >= 2 else ""
            candidate = build_product_candidate(
                keyword=keyword,
                base_url=self.page.url,
                detail_href=detail_href or "",
                gtin=gtin,
                row_text=row.inner_text(),
                position=position,
            )
            if candidate and candidate.detail_url not in seen_urls:
                seen_urls.add(candidate.detail_url)
                candidates.append(candidate)
        return candidates

    def _current_product_row_count(self) -> int:
        rows = self.page.locator(ROWS_SELECTOR)
        return sum(
            rows.nth(position).locator(DETAIL_BUTTON_SELECTOR).count() > 0
            for position in range(rows.count())
        )

    def _open_candidate(self, candidate: ProductCandidate) -> FrameLocator:
        previous_src = self._visible_frame_src()
        self.page.goto(candidate.detail_url, wait_until="domcontentloaded")
        frame_element = self.page.locator("#product-info-frame")
        try:
            frame_element.wait_for(state="visible", timeout=self.timeout_ms)
            if previous_src:
                self.page.wait_for_function(
                    """(oldSource) => {
                        const frame = document.querySelector("#product-info-frame");
                        return frame && frame.getAttribute("src") !== oldSource;
                    }""",
                    arg=previous_src,
                    timeout=self.timeout_ms,
                )
        except PlaywrightTimeoutError as error:
            raise NavigationError(
                f"Product iframe did not load for route: {candidate.detail_url}"
            ) from error
        return self.page.frame_locator("#product-info-frame")

    def _incoming_table_is_visible(self) -> bool:
        try:
            return self.page.locator(TABLE_SELECTOR).is_visible()
        except PlaywrightError:
            return False

    def _visible_frame_src(self) -> str:
        try:
            frame = self.page.locator("#product-info-frame")
            return frame.get_attribute("src") if frame.is_visible() else ""
        except PlaywrightError:
            return ""

    def _wait_for_table(self) -> None:
        try:
            self.page.locator(TABLE_SELECTOR).wait_for(
                state="visible", timeout=self.timeout_ms
            )
            self.page.locator(SEARCH_SELECTOR).first.wait_for(
                state="visible", timeout=self.timeout_ms
            )
            self.page.locator(LENGTH_SELECTOR).first.wait_for(
                state="visible", timeout=self.timeout_ms
            )
            self.page.locator(ROWS_SELECTOR).first.wait_for(
                state="attached", timeout=self.timeout_ms
            )
        except PlaywrightTimeoutError as error:
            raise NavigationError("Incoming products table did not become available.") from error

    def _read_table_state(self) -> TableState:
        return TableState(
            search=self.page.locator(SEARCH_SELECTOR).first.input_value(),
            length=self.page.locator(LENGTH_SELECTOR).first.input_value(),
            info=self.page.locator(INFO_SELECTOR).inner_text(),
            body_text=self.page.locator(f"{TABLE_SELECTOR} tbody").inner_text(),
        )

    def _wait_for_table_update(
        self,
        *,
        expected_search: str,
        expected_length: str,
        previous_state: TableState,
        allow_unchanged: bool,
    ) -> None:
        try:
            self.page.wait_for_function(
                """({ expectedSearch, expectedLength, previous, allowUnchanged }) => {
                    const input = document.querySelector("input[aria-controls='dt_incoming_products']");
                    const lengthSelect = document.querySelector(
                        "select[name='dt_incoming_products_length']"
                    );
                    const processing = document.querySelector("#dt_incoming_products_processing");
                    const info = document.querySelector("#dt_incoming_products_info");
                    const body = document.querySelector("#dt_incoming_products tbody");
                    const processingVisible = processing
                        && window.getComputedStyle(processing).display !== "none";
                    const infoText = (info && info.innerText) || "";
                    const bodyText = (body && body.innerText) || "";
                    const changed = infoText !== previous.info || bodyText !== previous.body_text;
                    return input
                        && lengthSelect
                        && input.value === expectedSearch
                        && lengthSelect.value === expectedLength
                        && !processingVisible
                        && (changed || allowUnchanged);
                }""",
                arg={
                    "expectedSearch": expected_search,
                    "expectedLength": expected_length,
                    "previous": {
                        "info": previous_state.info,
                        "body_text": previous_state.body_text,
                    },
                    "allowUnchanged": allow_unchanged,
                },
                timeout=self.timeout_ms,
            )
            self.page.locator(ROWS_SELECTOR).first.wait_for(
                state="attached", timeout=self.timeout_ms
            )
        except PlaywrightTimeoutError as error:
            raise NavigationError("Incoming-products table did not finish updating.") from error

    def _go_to_next_page(self) -> bool:
        control = self.page.locator(NEXT_SELECTOR).first
        if control.count() == 0:
            return False
        classes = control.get_attribute("class") or ""
        if "disabled" in classes or control.get_attribute("aria-disabled") == "true":
            return False

        previous_state = self._read_table_state()
        control.click()
        self._wait_for_table_update(
            expected_search=previous_state.search,
            expected_length=previous_state.length,
            previous_state=previous_state,
            allow_unchanged=False,
        )
        return True
