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
from .searches import ResultPolicy


TABLE_SELECTOR = "#dt_incoming_products"
SEARCH_SELECTOR = "input[aria-controls='dt_incoming_products']"
LENGTH_SELECTOR = "select[name='dt_incoming_products_length']"
ROWS_SELECTOR = f"{TABLE_SELECTOR} tbody tr"
EMPTY_RESULT_SELECTOR = f"{ROWS_SELECTOR} td.dataTables_empty"
INFO_SELECTOR = "#dt_incoming_products_info"
PROCESSING_SELECTOR = "#dt_incoming_products_processing"
INCOMING_PRODUCTS_LINK_SELECTOR = "a[ui-sref='app.task.incoming_products']"
DETAIL_BUTTON_SELECTOR = "button[ui-sref*='app.task.product_info'][href]"
NEXT_SELECTOR = "#dt_incoming_products_next"
TABLE_STATE_STORAGE_PREFIX = "DataTables_dt_incoming_products"

TABLE_STATE_CLEAR_SCRIPT = r"""
(prefix) => {
  const removed = [];
  for (let index = window.localStorage.length - 1; index >= 0; index -= 1) {
    const key = window.localStorage.key(index);
    if (key && key.startsWith(prefix)) {
      removed.push(key);
      window.localStorage.removeItem(key);
    }
  }
  return removed;
}
"""

DATA_TABLE_SEARCH_SCRIPT = r"""
(keyword) => {
  const table = document.querySelector("#dt_incoming_products");
  const input = document.querySelector(
    "input[aria-controls='dt_incoming_products']"
  );
  const $ = window.jQuery;
  if (!table || !input || !$ || !$.fn || !$.fn.dataTable
      || !$.fn.dataTable.isDataTable(table)) {
    return false;
  }
  input.value = keyword;
  $(table).DataTable().search(keyword).draw();
  return true;
}
"""

DATA_TABLE_LENGTH_SCRIPT = r"""
(length) => {
  const table = document.querySelector("#dt_incoming_products");
  const select = document.querySelector(
    "select[name='dt_incoming_products_length']"
  );
  const $ = window.jQuery;
  if (!table || !select || !$ || !$.fn || !$.fn.dataTable
      || !$.fn.dataTable.isDataTable(table)) {
    return false;
  }
  select.value = String(length);
  $(table).DataTable().page.len(Number(length)).draw();
  return true;
}
"""

TABLE_DIAGNOSTICS_SCRIPT = r"""
() => {
  const table = document.querySelector("#dt_incoming_products");
  const input = document.querySelector(
    "input[aria-controls='dt_incoming_products']"
  );
  const length = document.querySelector(
    "select[name='dt_incoming_products_length']"
  );
  const $ = window.jQuery;
  let dataTableReady = false;
  let dataTableSearch = null;
  let dataTableLength = null;
  try {
    dataTableReady = Boolean(
      table && $ && $.fn && $.fn.dataTable && $.fn.dataTable.isDataTable(table)
    );
    if (dataTableReady) {
      const api = $(table).DataTable();
      dataTableSearch = api.search();
      dataTableLength = api.page.len();
    }
  } catch (error) {
    dataTableReady = false;
  }
  return {
    inputValue: input ? input.value : null,
    lengthValue: length ? length.value : null,
    dataTableReady,
    dataTableSearch,
    dataTableLength,
  };
}
"""

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
        """Restore the incoming-products table after a product-detail route."""

        if self._incoming_table_is_visible():
            return
        self._clear_saved_table_state()
        if self._open_via_sidebar():
            return
        self._open_via_hard_reload()

    def _open_via_sidebar(self) -> bool:
        """Use GS1's SPA sidebar route before falling back to a document reload."""

        try:
            link = self.page.locator(INCOMING_PRODUCTS_LINK_SELECTOR).first
            if link.count() == 0 or not link.is_visible():
                return False
            link.click()
            self._wait_for_incoming_route_and_table()
            return True
        except (NavigationError, PlaywrightError):
            return False

    def _open_via_hard_reload(self) -> None:
        """Force the route to remount when an SPA sidebar transition did not render."""

        try:
            self._clear_saved_table_state()
            self.page.goto(INCOMING_PRODUCTS_URL, wait_until="domcontentloaded")
            # A same-document hash navigation can leave Angular's view stale.
            self.page.reload(wait_until="domcontentloaded")
            self._wait_for_incoming_route_and_table()
        except (NavigationError, PlaywrightError) as error:
            raise self._navigation_error(
                "hard_reload",
                "Incoming products could not be restored after route recovery.",
            ) from error

    def _wait_for_incoming_route_and_table(self) -> None:
        try:
            self.page.wait_for_function(
                """() => window.location.hash.startsWith(
                    '#/app/task/incoming-products'
                )""",
                timeout=self.timeout_ms,
            )
        except PlaywrightError as error:
            raise self._navigation_error(
                "incoming_route",
                "Incoming-products route did not become active.",
            ) from error
        self._wait_for_table()

    def process_keyword(
        self,
        keyword: str,
        on_product: Callable[[FrameLocator, ProductCandidate], None],
        on_failure: Callable[[ProductCandidate, Exception], None],
        on_attempt: Callable[[ProductCandidate], None],
        should_stop: Callable[[], bool],
    ) -> KeywordResult:
        """Compatibility wrapper for the existing all-results keyword flow."""

        return self.process_search(
            query=keyword,
            result_policy=ResultPolicy.ALL,
            on_product=on_product,
            on_failure=on_failure,
            on_attempt=on_attempt,
            should_stop=should_stop,
        )

    def process_search(
        self,
        query: str,
        result_policy: ResultPolicy,
        on_product: Callable[[FrameLocator, ProductCandidate], None],
        on_failure: Callable[[ProductCandidate, Exception], None],
        on_attempt: Callable[[ProductCandidate], None],
        should_stop: Callable[[], bool],
    ) -> KeywordResult:
        """Find and visit either every result or only the first visible result."""

        candidates = self._prepare_candidates(query, result_policy)
        print(f"Found {len(candidates)} candidate products for: {query}")

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

    def _prepare_candidates(
        self,
        keyword: str,
        result_policy: ResultPolicy = ResultPolicy.ALL,
    ) -> list[ProductCandidate]:
        """Prepare one search, retrying a broken table exactly once."""

        try:
            return self._prepare_candidates_once(keyword, result_policy)
        except NavigationError as initial_error:
            print(f"Retrying incoming-products table for: {keyword}")
            try:
                self._open_via_hard_reload()
                return self._search_and_collect_candidates(keyword, result_policy)
            except NavigationError as recovery_error:
                raise self._navigation_error(
                    "keyword_retry",
                    f"Could not prepare keyword {keyword!r} after one table recovery. "
                    f"Initial failure: {initial_error}",
                ) from recovery_error

    def _prepare_candidates_once(
        self,
        keyword: str,
        result_policy: ResultPolicy = ResultPolicy.ALL,
    ) -> list[ProductCandidate]:
        self.open()
        return self._search_and_collect_candidates(keyword, result_policy)

    def _search_and_collect_candidates(
        self,
        keyword: str,
        result_policy: ResultPolicy = ResultPolicy.ALL,
    ) -> list[ProductCandidate]:
        try:
            self.apply_search(keyword)
            if result_policy is ResultPolicy.FIRST:
                return self._snapshot_current_page_candidates(keyword)[:1]
            return self._collect_candidates(keyword)
        except PlaywrightError as error:
            raise self._navigation_error(
                "keyword_search",
                "Incoming-products table operation failed.",
            ) from error

    def apply_search(self, keyword: str) -> None:
        self._wait_for_table()
        previous_state = self._read_table_state()
        update_token = self._start_table_update_tracker()
        used_data_table_api = self._set_data_table_search(keyword)
        if not used_data_table_api:
            search_input = self.page.locator(SEARCH_SELECTOR).first
            search_input.fill(keyword)
            search_input.press("Enter")
        self._wait_for_table_update(
            expected_search=keyword,
            expected_length=previous_state.length,
            previous_state=previous_state,
            update_token=update_token,
            allow_unchanged=False,
            require_redraw=True,
            require_data_table_search=used_data_table_api,
            phase="search",
        )

    def show_all_results(self) -> bool:
        """Choose DataTables' all-results option and confirm every matching row rendered."""

        self._wait_for_table()
        length_select = self.page.locator(LENGTH_SELECTOR).first
        if length_select.input_value() != "-1":
            previous_state = self._read_table_state()
            update_token = self._start_table_update_tracker()
            used_data_table_api = self._set_data_table_length(-1)
            if not used_data_table_api:
                length_select.select_option("-1")
            self._wait_for_table_update(
                expected_search=previous_state.search,
                expected_length="-1",
                previous_state=previous_state,
                update_token=update_token,
                allow_unchanged=False,
                require_redraw=True,
                require_data_table_search=False,
                phase="show_all_results",
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
            raise self._navigation_error(
                "paginate",
                "GS1 returned fewer product rows than its table total; "
                "refusing an incomplete export.",
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
        """Return true only for a fully usable incoming-products table view."""

        if not urlparse(self.page.url).fragment.startswith(
            "/app/task/incoming-products"
        ):
            return False
        try:
            return all(
                self.page.locator(selector).first.is_visible()
                for selector in (
                    TABLE_SELECTOR,
                    SEARCH_SELECTOR,
                    LENGTH_SELECTOR,
                    ROWS_SELECTOR,
                )
            )
        except PlaywrightError:
            return False

    def _visible_frame_src(self) -> str:
        try:
            frame = self.page.locator("#product-info-frame")
            return frame.get_attribute("src") if frame.is_visible() else ""
        except PlaywrightError:
            return ""

    def _clear_saved_table_state(self) -> None:
        """Remove only this table's DataTables state before rebuilding its route."""

        try:
            self.page.evaluate(TABLE_STATE_CLEAR_SCRIPT, TABLE_STATE_STORAGE_PREFIX)
        except PlaywrightError as error:
            raise self._navigation_error(
                "clear_saved_table_state",
                "Could not clear saved incoming-products table state.",
            ) from error

    def _set_data_table_search(self, keyword: str) -> bool:
        """Use DataTables directly so every search creates a server-side draw."""

        try:
            return bool(self.page.evaluate(DATA_TABLE_SEARCH_SCRIPT, keyword))
        except PlaywrightError:
            return False

    def _set_data_table_length(self, length: int) -> bool:
        """Use DataTables directly when expanding the server-side result set."""

        try:
            return bool(self.page.evaluate(DATA_TABLE_LENGTH_SCRIPT, length))
        except PlaywrightError:
            return False

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
        except PlaywrightError as error:
            raise self._navigation_error(
                "wait_for_table",
                "Incoming products table did not become available.",
            ) from error

    def _read_table_state(self) -> TableState:
        return TableState(
            search=self.page.locator(SEARCH_SELECTOR).first.input_value(),
            length=self.page.locator(LENGTH_SELECTOR).first.input_value(),
            info=self.page.locator(INFO_SELECTOR).inner_text(),
            body_text=self.page.locator(f"{TABLE_SELECTOR} tbody").inner_text(),
        )

    def _start_table_update_tracker(self) -> int:
        """Track DataTables draws even when two zero-result messages have identical text."""

        try:
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
        except PlaywrightError as error:
            raise self._navigation_error(
                "start_table_update_tracker",
                "Could not observe the incoming-products table redraw.",
            ) from error

    def _wait_for_table_update(
        self,
        *,
        expected_search: str,
        expected_length: str,
        previous_state: TableState,
        update_token: int,
        allow_unchanged: bool,
        require_redraw: bool,
        require_data_table_search: bool,
        phase: str,
    ) -> None:
        try:
            self.page.wait_for_function(
                """({ expectedSearch, expectedLength, previous, updateToken,
                      allowUnchanged, requireRedraw, requireDataTableSearch }) => {
                    const table = document.querySelector("#dt_incoming_products");
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
                    const redrawn = Boolean(tracker && tracker.version > updateToken);
                    const $ = window.jQuery;
                    let dataTableReady = false;
                    let dataTableSearch = null;
                    try {
                        dataTableReady = Boolean(
                            table && $ && $.fn && $.fn.dataTable
                            && $.fn.dataTable.isDataTable(table)
                        );
                        if (dataTableReady) {
                            dataTableSearch = $(table).DataTable().search();
                        }
                    } catch (error) {
                        dataTableReady = false;
                    }
                    const updateFinished = requireRedraw
                        ? redrawn
                        : (changed || redrawn || allowUnchanged);
                    return input
                        && lengthSelect
                        && input.value === expectedSearch
                        && lengthSelect.value === expectedLength
                        && !processingVisible
                        && updateFinished
                        && (!requireDataTableSearch
                            || (dataTableReady && dataTableSearch === expectedSearch));
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
                    "requireRedraw": require_redraw,
                    "requireDataTableSearch": require_data_table_search,
                },
                timeout=self.timeout_ms,
            )
            self.page.locator(ROWS_SELECTOR).first.wait_for(
                state="attached", timeout=self.timeout_ms
            )
        except PlaywrightError as error:
            raise self._navigation_error(
                phase,
                "Incoming-products table did not finish updating.",
            ) from error

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
            require_redraw=True,
            require_data_table_search=False,
            phase="next_page",
        )
        return True

    def _navigation_error(self, phase: str, message: str) -> NavigationError:
        return NavigationError(f"{message} {self._table_diagnostics(phase)}")

    def _table_diagnostics(self, phase: str) -> str:
        """Capture safe table state in navigation errors without exposing credentials."""

        try:
            diagnostics = self.page.evaluate(TABLE_DIAGNOSTICS_SCRIPT)
        except PlaywrightError:
            diagnostics = None

        if not isinstance(diagnostics, dict):
            return f"phase={phase}; url={self.page.url}; table_diagnostics=unavailable."

        def text_value(name: str) -> str:
            value = diagnostics.get(name)
            return clean_text(value) if isinstance(value, str) else ""

        return (
            f"phase={phase}; url={self.page.url}; "
            f"input={text_value('inputValue')!r}; "
            f"length={text_value('lengthValue')!r}; "
            f"data_table_ready={bool(diagnostics.get('dataTableReady'))}; "
            f"data_table_search={text_value('dataTableSearch')!r}; "
            f"data_table_length={diagnostics.get('dataTableLength')!r}."
        )
