"""High-level orchestration of the authenticated GS1 export."""

from __future__ import annotations

from dataclasses import dataclass

from playwright.sync_api import FrameLocator, Page, sync_playwright

from .auth import prepare_manual_login
from .config import ScraperSettings
from .detail_parser import ProductDetailParser
from .models import (
    ProductRecord,
    product_types_from_text,
    with_barcode_fallback,
    with_text_fallback,
)
from .navigation import IncomingProductsNavigator, NavigationError, ProductCandidate
from .searches import SearchKind, SearchTask, keyword_search_tasks
from .storage import ErrorLogger, ProductStore


@dataclass
class RunSummary:
    attempted: int = 0
    exported: int = 0
    duplicates: int = 0
    missing_barcodes: int = 0
    issues: int = 0
    completed_searches: int = 0

    @property
    def completed_keywords(self) -> int:
        """Retain the prior summary attribute for callers of keyword mode."""

        return self.completed_searches


class Gs1Scraper:
    def __init__(
        self,
        settings: ScraperSettings,
        store: ProductStore,
        error_logger: ErrorLogger,
    ) -> None:
        self.settings = settings
        self.store = store
        self.error_logger = error_logger
        self.detail_parser = ProductDetailParser()

    def run(self, searches: list[SearchTask] | list[str]) -> RunSummary:
        tasks = self._normalize_searches(searches)
        summary = RunSummary()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=False)
            context = browser.new_context()
            page = context.new_page()
            try:
                prepare_manual_login(
                    page,
                    self.settings.credentials,
                    self.settings.login_timeout_ms,
                )
                navigator = IncomingProductsNavigator(page)

                for task in tasks:
                    if task.checkpoint_key in self.store.completed_searches:
                        print(f"Skipping completed search: {task.query}")
                        summary.completed_searches += 1
                        continue

                    print(f"Searching: {task.query}")
                    try:
                        result = navigator.process_search(
                            query=task.query,
                            result_policy=task.result_policy,
                            on_product=lambda frame, candidate: self._handle_product(
                                frame, candidate, task, summary
                            ),
                            on_failure=lambda candidate, error: self._handle_failure(
                                candidate, error, summary
                            ),
                            on_attempt=lambda _candidate: self._record_attempt(summary),
                            should_stop=lambda: self._limit_reached(summary),
                        )
                    except NavigationError as error:
                        summary.issues += 1
                        self.error_logger.write(
                            event=f"{task.kind.value}_navigation_failure",
                            keyword=task.query,
                            barcode=task.expected_barcode,
                            message=f"{type(error).__name__}: {error}",
                        )
                        print(f"Skipping unavailable search for now: {task.query}")
                        continue
                    if result.completed:
                        if task.kind is SearchKind.PRODUCT and result.discovered == 0:
                            self._record_product_not_found(task, summary)
                        self.store.mark_search_complete(task.checkpoint_key)
                        summary.completed_searches += 1
                    else:
                        print("Product limit reached; progress was saved for a later resume.")
                        break
            except Exception:
                self._save_failure_screenshot(page)
                raise
            finally:
                context.close()
                browser.close()
        return summary

    @staticmethod
    def _normalize_searches(
        searches: list[SearchTask] | list[str],
    ) -> list[SearchTask]:
        if all(isinstance(search, str) for search in searches):
            return keyword_search_tasks([str(search) for search in searches])
        if not all(isinstance(search, SearchTask) for search in searches):
            raise TypeError("Searches must contain only SearchTask values or only strings")
        return list(searches)

    def _handle_product(
        self,
        frame: FrameLocator,
        candidate: ProductCandidate,
        task: SearchTask,
        summary: RunSummary,
    ) -> None:
        extracted = with_text_fallback(
            with_barcode_fallback(
                self.detail_parser.extract_from_frame(frame), candidate.barcode_hint
            ),
            candidate.source_description,
        )
        product_types: tuple[str, ...] = ()
        if task.kind is SearchKind.KEYWORD:
            product_types = product_types_from_text(
                candidate.source_gpc,
                extracted.name,
                extracted.description,
                extracted.short_description,
            )
            if not product_types:
                product_types = product_types_from_text(candidate.keyword)
        record = ProductRecord.from_extracted(
            extracted,
            image=candidate.image_url,
            product_types=product_types,
        )
        if not record.barcode:
            summary.missing_barcodes += 1
            summary.issues += 1
            self.error_logger.write(
                event="missing_barcode",
                keyword=candidate.keyword,
                row_signature=candidate.signature,
                message="Product detail did not expose a valid 8-14 digit barcode.",
            )
            return

        if (
            task.kind is SearchKind.PRODUCT
            and task.expected_barcode
            and record.barcode != task.expected_barcode
        ):
            summary.issues += 1
            self.error_logger.write(
                event="product_barcode_mismatch",
                keyword=task.query,
                barcode=record.barcode,
                row_signature=candidate.signature,
                message=(
                    f"Workbook barcode {task.expected_barcode} differs from the first "
                    f"GS1 result barcode {record.barcode}; exported the GS1 result."
                ),
            )

        result = self.store.write(record)
        if result.written:
            summary.exported += 1
        elif result.reason in {"duplicate_barcode", "merged_duplicate"}:
            summary.duplicates += 1
        else:
            summary.missing_barcodes += 1

        if missing_fields := extracted.missing_text_fields:
            summary.issues += 1
            self.error_logger.write(
                event="missing_text_fields",
                keyword=candidate.keyword,
                barcode=record.barcode,
                row_signature=candidate.signature,
                message="Missing: " + ", ".join(missing_fields),
            )

    def _record_product_not_found(
        self,
        task: SearchTask,
        summary: RunSummary,
    ) -> None:
        summary.issues += 1
        self.error_logger.write(
            event="product_not_found",
            keyword=task.query,
            barcode=task.expected_barcode,
            message="No GS1 results were found for the workbook product name.",
        )

    def _handle_failure(
        self,
        candidate: ProductCandidate,
        error: Exception,
        summary: RunSummary,
    ) -> None:
        summary.issues += 1
        self.error_logger.write(
            event="product_extraction_failure",
            keyword=candidate.keyword,
            row_signature=candidate.signature,
            message=f"{type(error).__name__}: {error}",
        )

    @staticmethod
    def _record_attempt(summary: RunSummary) -> None:
        summary.attempted += 1

    def _limit_reached(self, summary: RunSummary) -> bool:
        return (
            self.settings.max_products is not None
            and summary.attempted >= self.settings.max_products
        )

    def _save_failure_screenshot(self, page: Page) -> None:
        screenshot_path = self.settings.output_path.with_name("gs1_scraper_failure.png")
        try:
            screenshot_path.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(screenshot_path), full_page=True)
        except Exception:
            # The original failure is more useful than a screenshot failure.
            pass


# Keep the original class name import-compatible for existing callers.
Gs1AlcoholScraper = Gs1Scraper
