"""High-level orchestration of the authenticated GS1 export."""

from __future__ import annotations

from dataclasses import dataclass

from playwright.sync_api import FrameLocator, Page, sync_playwright

from .auth import prepare_manual_login
from .config import ScraperSettings
from .detail_parser import ProductDetailParser
from .models import ProductRecord, with_barcode_fallback
from .navigation import IncomingProductsNavigator, ProductCandidate
from .storage import ErrorLogger, ProductStore


@dataclass
class RunSummary:
    attempted: int = 0
    exported: int = 0
    duplicates: int = 0
    missing_barcodes: int = 0
    issues: int = 0
    completed_keywords: int = 0


class Gs1AlcoholScraper:
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

    def run(self, keywords: list[str]) -> RunSummary:
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
                navigator.open()

                for keyword in keywords:
                    if keyword in self.store.completed_keywords:
                        print(f"Skipping completed keyword: {keyword}")
                        summary.completed_keywords += 1
                        continue

                    print(f"Searching: {keyword}")
                    result = navigator.process_keyword(
                        keyword=keyword,
                        on_product=lambda frame, candidate: self._handle_product(
                            frame, candidate, summary
                        ),
                        on_failure=lambda candidate, error: self._handle_failure(
                            candidate, error, summary
                        ),
                        on_attempt=lambda _candidate: self._record_attempt(summary),
                        should_stop=lambda: self._limit_reached(summary),
                    )
                    if result.completed:
                        self.store.mark_keyword_complete(keyword)
                        summary.completed_keywords += 1
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

    def _handle_product(
        self,
        frame: FrameLocator,
        candidate: ProductCandidate,
        summary: RunSummary,
    ) -> None:
        extracted = with_barcode_fallback(
            self.detail_parser.extract_from_frame(frame), candidate.barcode_hint
        )
        record = ProductRecord.from_extracted(extracted)
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

        result = self.store.write(record)
        if result.written:
            summary.exported += 1
        elif result.reason == "duplicate_barcode":
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
