"""Read an RFQ workbook (spec section 6.4) into ``RFQLine`` objects."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any

from openpyxl import load_workbook
from pydantic import BaseModel

from rfq_agent.partno import normalize_part_no
from rfq_agent.schemas import RFQLine

REQUIRED = "customer_part_no"
KNOWN_COLUMNS = (REQUIRED, "our_part_no", "description", "annual_qty", "customer_name")


class IntakeError(ValueError):
    """The workbook cannot be parsed (e.g. required column missing)."""


class SkippedRow(BaseModel):
    excel_row: int
    reason: str


class IntakeResult(BaseModel):
    lines: list[RFQLine]
    skipped: list[SkippedRow]


def normalize_header(value: Any) -> str:
    """``" Customer Part No "`` -> ``"customer_part_no"``."""
    text = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    return re.sub(r"[\s\-]+", "_", text)


def read_rfq(path: Path) -> IntakeResult:
    """Parse the first sheet. Blank rows are ignored; row_id counts non-blank data rows."""
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb.worksheets[0]
        rows = ws.iter_rows(values_only=True)
        header = next(rows, None)
        if header is None:
            raise IntakeError(f"{path} is empty")
        columns = {normalize_header(h): i for i, h in enumerate(header) if h is not None}
        if REQUIRED not in columns:
            raise IntakeError(f"{path} has no customer_part_no column (got {sorted(columns)})")

        lines: list[RFQLine] = []
        skipped: list[SkippedRow] = []
        for excel_row, values in enumerate(rows, start=2):
            if all(_clean(v) is None for v in values):
                continue
            record = {
                name: _clean(values[idx]) if idx < len(values) else None
                for name, idx in columns.items()
                if name in KNOWN_COLUMNS
            }
            raw = record.get(REQUIRED)
            normalized = normalize_part_no(raw) if raw else ""
            if not normalized:
                skipped.append(SkippedRow(excel_row=excel_row, reason="empty customer_part_no"))
                continue
            our = record.get("our_part_no")
            lines.append(
                RFQLine(
                    row_id=len(lines) + 1,
                    customer_part_no_raw=str(values[columns[REQUIRED]]),
                    customer_part_no=normalized,
                    our_part_no=unicodedata.normalize("NFKC", our).upper() if our else None,
                    description=record.get("description"),
                    annual_qty=_to_int(record.get("annual_qty")),
                    customer_name=record.get("customer_name"),
                )
            )
        return IntakeResult(lines=lines, skipped=skipped)
    finally:
        wb.close()


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = unicodedata.normalize("NFKC", str(value)).strip()
    return text or None


def _to_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        number = float(value.replace(",", ""))
    except ValueError:
        return None
    return int(number) if number.is_integer() and number >= 0 else None
