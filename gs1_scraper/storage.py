"""Incremental CSV output, deduplication, checkpoints, and issue logging."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .models import CSV_COLUMNS, ProductRecord, normalize_barcode


def checkpoint_path_for(output_path: Path) -> Path:
    return output_path.with_name(f"{output_path.stem}.checkpoint.json")


def error_path_for(output_path: Path) -> Path:
    return output_path.with_name(f"{output_path.stem}.errors.csv")


def _write_csv_row(path: Path, columns: tuple[str, ...], row: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists() or path.stat().st_size == 0
    encoding = "utf-8-sig" if is_new else "utf-8"
    with path.open("a", encoding=encoding, newline="") as file_handle:
        writer = csv.DictWriter(file_handle, fieldnames=columns)
        if is_new:
            writer.writeheader()
        writer.writerow(row)


@dataclass
class Checkpoint:
    path: Path
    exported_barcodes: set[str] = field(default_factory=set)
    completed_keywords: set[str] = field(default_factory=set)

    @classmethod
    def load(cls, path: Path) -> "Checkpoint":
        if not path.exists():
            return cls(path=path)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"Could not read checkpoint {path}: {error}") from error

        return cls(
            path=path,
            exported_barcodes={
                barcode
                for value in payload.get("exported_barcodes", [])
                if (barcode := normalize_barcode(str(value)))
            },
            completed_keywords={
                str(value) for value in payload.get("completed_keywords", []) if str(value)
            },
        )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "exported_barcodes": sorted(self.exported_barcodes),
            "completed_keywords": sorted(self.completed_keywords),
        }
        temporary_path = self.path.with_name(f"{self.path.name}.tmp")
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary_path.replace(self.path)


class ErrorLogger:
    columns = ("timestamp", "event", "keyword", "barcode", "row_signature", "message")

    def __init__(self, output_path: Path) -> None:
        self.path = error_path_for(output_path)

    def write(
        self,
        event: str,
        keyword: str,
        message: str,
        barcode: str = "",
        row_signature: str = "",
    ) -> None:
        _write_csv_row(
            self.path,
            self.columns,
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": event,
                "keyword": keyword,
                "barcode": barcode,
                "row_signature": row_signature,
                "message": message,
            },
        )


@dataclass(frozen=True)
class WriteResult:
    written: bool
    reason: str = ""


class ProductStore:
    """Use CSV as the durable record and JSON as fast resume metadata."""

    def __init__(self, output_path: Path) -> None:
        self.output_path = output_path
        self.checkpoint = Checkpoint.load(checkpoint_path_for(output_path))
        self.seen_barcodes = set(self.checkpoint.exported_barcodes)
        self._load_existing_barcodes()

    @property
    def completed_keywords(self) -> set[str]:
        return self.checkpoint.completed_keywords

    def _load_existing_barcodes(self) -> None:
        if not self.output_path.exists() or self.output_path.stat().st_size == 0:
            return
        with self.output_path.open("r", encoding="utf-8-sig", newline="") as file_handle:
            reader = csv.DictReader(file_handle)
            if tuple(reader.fieldnames or ()) != CSV_COLUMNS:
                raise ValueError(
                    f"Existing CSV header does not match the expected schema: {self.output_path}"
                )
            for row in reader:
                if barcode := normalize_barcode(row.get("barcode")):
                    self.seen_barcodes.add(barcode)

    def write(self, record: ProductRecord) -> WriteResult:
        barcode = normalize_barcode(record.barcode)
        if not barcode:
            return WriteResult(written=False, reason="missing_barcode")
        if barcode in self.seen_barcodes:
            return WriteResult(written=False, reason="duplicate_barcode")

        _write_csv_row(self.output_path, CSV_COLUMNS, record.to_csv_row())
        self.seen_barcodes.add(barcode)
        self.checkpoint.exported_barcodes.add(barcode)
        self.checkpoint.save()
        return WriteResult(written=True)

    def mark_keyword_complete(self, keyword: str) -> None:
        self.checkpoint.completed_keywords.add(keyword)
        self.checkpoint.save()

    @staticmethod
    def reset(output_path: Path) -> None:
        for path in (output_path, checkpoint_path_for(output_path), error_path_for(output_path)):
            path.unlink(missing_ok=True)
