from __future__ import annotations

import pytest

from rfq_agent.partno import normalize_part_no


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ｋｒ 21200-4a7 ", "KR212004A7"),
        ("  TS - 40213 ", "TS40213"),
        ("1234.56.789", "123456789"),
        ("ab_12/c", "AB12C"),
        ("ＷＰ－８８１２３", "WP88123"),
        ("", ""),
    ],
)
def test_normalize(raw: str, expected: str) -> None:
    assert normalize_part_no(raw) == expected
