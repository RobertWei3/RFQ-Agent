from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import Workbook

from rfq_agent.intake import IntakeError, read_rfq
from rfq_agent.intake.reader import normalize_header


def _write(path: Path, rows: list[list[object]]) -> Path:
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    for row in rows:
        ws.append(row)
    wb.save(path)
    return path


def test_header_normalization() -> None:
    assert normalize_header(" Customer Part No ") == "customer_part_no"
    assert normalize_header("ANNUAL-QTY") == "annual_qty"
    assert normalize_header("ｏｕｒ　ｐａｒｔ　ｎｏ") == "our_part_no"


def test_reads_dirty_workbook(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "rfq.xlsx",
        [
            ["CUSTOMER PART NO", "our part no", "Description", "Annual  Qty", "Customer Name", "x"],
            ["  ＴＳ－４０２１３ ", None, "Thermostat", "1,200", " Acme ", "ignored"],
            [None, None, None, None, None, None],
            ["wp-88123-a", "wpm-20001", None, "n/a", None, None],
            ["  -  ", None, "no part number", 5, None, None],
            ["WP 1", None, None, 12.0, None, None],
        ],
    )
    result = read_rfq(path)
    assert [line.row_id for line in result.lines] == [1, 2, 3]
    first, second, third = result.lines
    assert first.customer_part_no_raw == "  ＴＳ－４０２１３ "
    assert first.customer_part_no == "TS40213"
    assert first.annual_qty == 1200
    assert first.customer_name == "Acme"
    assert second.customer_part_no == "WP88123A"
    assert second.our_part_no == "WPM-20001"
    assert second.annual_qty is None
    assert third.annual_qty == 12
    assert [s.excel_row for s in result.skipped] == [5]


def test_missing_required_column(tmp_path: Path) -> None:
    path = _write(tmp_path / "bad.xlsx", [["Part", "Qty"], ["A", 1]])
    with pytest.raises(IntakeError, match="customer_part_no"):
        read_rfq(path)


def test_generated_workbook(generated_dataset: Path) -> None:
    result = read_rfq(generated_dataset / "rfq_200.xlsx")
    assert len(result.lines) == 200
    assert not result.skipped
