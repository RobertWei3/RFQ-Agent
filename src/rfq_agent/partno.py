"""Part-number normalization shared by intake, tools, MCP server and matching."""

from __future__ import annotations

import re
import unicodedata

_SEPARATORS = re.compile(r"[\s\-_./]+")


def normalize_part_no(raw: str) -> str:
    """NFKC -> strip -> upper -> drop spaces and ``- _ . /``.

    Example: ``"ｋｒ 21200-4a7 "`` -> ``"KR212004A7"``.
    """
    text = unicodedata.normalize("NFKC", raw).strip().upper()
    return _SEPARATORS.sub("", text)
