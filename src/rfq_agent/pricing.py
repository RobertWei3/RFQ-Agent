"""Price comparison shared by confidence scoring and evaluation."""

from __future__ import annotations

MSRP_TOLERANCE = 0.01  # spec sections 12.2 and 14: relative error <= 1% counts as the same


def msrp_matches(value: float, reference: float) -> bool:
    """``value`` is within the MSRP tolerance of ``reference`` (relative to ``reference``)."""
    return abs(value - reference) <= MSRP_TOLERANCE * abs(reference)
