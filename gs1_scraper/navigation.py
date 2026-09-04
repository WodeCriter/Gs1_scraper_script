"""Direct product-route traversal for the GS1 incoming-products DataTable."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from playwright.sync_api import (
    Error as PlaywrightError,
    FrameLocator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

from .config import INCOMING_PRODUCTS_URL
from .models import clean_text, normalize_barcode, row_signature


TABLE_SELECTOR = "#dt_incoming_products"
SEARCH_SELECTOR = "input[aria-controls='dt_incoming_products']"
LENGTH_SELECTOR = "select[name='dt_incoming_products_length']"
ROWS_SELECTOR = f"{TABLE_SELECTOR} tbody tr"
EMPTY_RESULT_SELECTOR = f"{ROWS_SELECTOR} td.dataTables_empty"
INFO_SELECTOR = "#dt_incoming_products_info"
PROCESSING_SELECTOR = "#dt_incoming_products_processing"
DETAIL_BUTTON_SELECTOR = "button[ui-sref*='app.task.product_info'][href]"
NEXT_SELECTOR = "#dt_incoming_products_next"

TABLE_SNAPSHOT_SCRIPT = r"""
(body) => {
  const clean = (value) => (value || "").replace(/\s+/g, " ").trim();
  const cellText = (cells, index) => clean(
    cells[index] && (cells[index].innerText || cells[index].textContent)
  );
  return Array.from(body.querySelectorAll(":scope > tr")).map((row, position) => {
    const cells = row.querySelectorAll(":scope > td");
    const button = row.querySelector(
      "button[ui-sref*='app.task.product_info'][href]"
    );
    const image = row.querySelector("td.prod-img img");
    return {
      position,
      detailHref: button && button.getAttribute("href"),
      gtin: cellText(cells, 1),
      rowText: clean(row.innerText || row.textContent),
      sourceDescription: cellText(cells, 8),
      imageSource: image && (
        image.getAttribute("src") || image.getAttribute("ng-src")
      ),
      sourceGpc: cellText(cells, 14),
    };
  });
}
"""


class NavigationError(RuntimeError):
    """Raised when the portal cannot be moved to a known product-table state."""


@dataclass(frozen=True)
class ProductCandidate:
    keyword: str
    detail_url: str
    barcode_hint: str
    signature: str
    source_description: str
    image_url: str
    source_gpc: str


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


def is_empty_result(total: int | None, product_row_count: int) -> bool:
    """Identify DataTables' normal zero-result state without treating it as a failure."""

    return total == 0 and product_row_count == 0


def normalize_image_url(base_url: str, image_source: str | None) -> str:
    """Keep only HTTP(S) image URLs and resolve relative GS1 paths."""

    candidate = clean_text(image_source)
    if not candidate:
        return ""
    resolved = urljoin(base_url, candidate)
    parsed = urlparse(resolved)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return resolved


def build_product_candidate(
    *,
    keyword: str,
    base_url: str,
    detail_href: str,
    gtin: str,
    row_text: str,
    position: int,
    source_description: str = "",
    image_source: str = "",
    source_gpc: str = "",
) -> ProductCandidate | None:
    """Build a direct-detail candidate from a table row without clicking it."""

    if not detail_href:
        return None
    return ProductCandidate(
        keyword=keyword,
        detail_url=urljoin(base_url, detail_href),
        barcode_hint=normalize_barcode(gtin),
        signature=row_signature(row_text, position),
        source_description=clean_text(source_description),
        image_url=normalize_image_url(base_url, image_source),
        source_gpc=clean_text(source_gpc),
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
        update_token = self._start_table_update_tracker()
        self.page.locator(SEARCH_SELECTOR).first.fill(keyword)
        self._wait_for_table_update(
            expected_search=keyword,
            expected_length=previous_state.length,
            previous_state=previous_state,
            update_token=update_token,
            allow_unchanged=previous_state.search == keyword,
        )

    def show_all_results(self) -> bool:
        """Choose DataTables' all-results option and confirm every matching row rendered."""

        self._wait_for_table()
        length_select = self.page.locator(LENGTH_SELECTOR).first
        if length_select.input_value() != "-1":
            previous_state = self._read_table_state()
            update_token = self._start_table_update_tracker()
            length_select.select_option("-1")
            total_before = table_total(previous_state.info)
            self._wait_for_table_update(
                expected_search=previous_state.search,
                expected_length="-1",
                previous_state=previous_state,
                update_token=update_token,
                # Fewer than 16 matches may render identically after the selection.
                allow_unchanged=total_before is not None and total_before <= 15,
            )

        total = table_total(self._read_table_state().info)
        if self._has_empty_result(total):
            return True
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
        raw_rows = self.page.locator(f"{TABLE_SELECTOR} tbody").evaluate(
            TABLE_SNAPSHOT_SCRIPT
        )
        if not isinstance(raw_rows, list):
            return []
        candidates: list[ProductCandidate] = []
        seen_urls: set[str] = set()
        for raw_row in raw_rows:
            if not isinstance(raw_row, dict):
                continue
            candidate = build_product_candidate(
                keyword=keyword,
                base_url=self.page.url,
                detail_href=str(raw_row.get("detailHref") or ""),
                gtin=str(raw_row.get("gtin") or ""),
                row_text=str(raw_row.get("rowText") or ""),
                position=int(raw_row.get("position") or 0),
                source_description=str(raw_row.get("sourceDescription") or ""),
                image_source=str(raw_row.get("imageSource") or ""),
                source_gpc=str(raw_row.get("sourceGpc") or ""),
            )
            if candidate and candidate.detail_url not in seen_urls:
                seen_urls.add(candidate.detail_url)
                candidates.append(candidate)
        return candidates

    def _current_product_row_count(self) -> int:
        return int(
            self.page.locator(f"{TABLE_SELECTOR} tbody").evaluate(
                """(body) => body.querySelectorAll(
                    "button[ui-sref*='app.task.product_info'][href]"
                ).length"""
            )
        )

    def _has_empty_result(self, total: int | None) -> bool:
        return (
            self.page.locator(EMPTY_RESULT_SELECTOR).count() > 0
            or is_empty_result(total, self._current_product_row_count())
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

    def _start_table_update_tracker(self) -> int:
        """Track DataTables draws even when two zero-result messages have identical text."""

        return int(
            self.page.evaluate(
                """() => {
                    const table = document.querySelector("#dt_incoming_products");
                    if (!table) return -1;
                    window.__gs1TableUpdateObserver?.disconnect();
                    const state = { version: 0 };
                    const advance = () => { state.version += 1; };
                    const observer = new MutationObserver(advance);
                    observer.observe(table, {
                        childList: true,
                        subtree: true,
                        characterData: true,
                    });
                    if (window.jQuery) {
                        window.jQuery(table)
                            .off("draw.dt.gs1Scraper")
                            .on("draw.dt.gs1Scraper", advance);
                    }
                    window.__gs1TableUpdateState = state;
                    window.__gs1TableUpdateObserver = observer;
                    return state.version;
                }"""
            )
        )

    def _wait_for_table_update(
        self,
        *,
        expected_search: str,
        expected_length: str,
        previous_state: TableState,
        update_token: int,
        allow_unchanged: bool,
    ) -> None:
        try:
            self.page.wait_for_function(
                """({ expectedSearch, expectedLength, previous, updateToken, allowUnchanged }) => {
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
                    const tracker = window.__gs1TableUpdateState;
                    const redrawn = tracker && tracker.version > updateToken;
                    return input
                        && lengthSelect
                        && input.value === expectedSearch
                        && lengthSelect.value === expectedLength
                        && !processingVisible
                        && (changed || redrawn || allowUnchanged);
                }""",
                arg={
                    "expectedSearch": expected_search,
                    "expectedLength": expected_length,
                    "previous": {
                        "info": previous_state.info,
                        "body_text": previous_state.body_text,
                    },
                    "updateToken": update_token,
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
        update_token = self._start_table_update_tracker()
        control.click()
        self._wait_for_table_update(
            expected_search=previous_state.search,
            expected_length=previous_state.length,
            previous_state=previous_state,
            update_token=update_token,
            allow_unchanged=False,
        )
        return True
