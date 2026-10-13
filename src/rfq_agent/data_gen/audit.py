"""Measure section 6.3 difficulty ratios from the generated artifacts themselves.

The manifest counts tags; this audit re-derives each difficulty from what was
actually written (pages, internal DB, workbook), so it checks the generator's
output rather than its bookkeeping. Evaluation-side code: it may read ground truth.
"""

from __future__ import annotations

import json
import re
import sqlite3
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from rfq_agent.data_gen.params import SECTION_6_3, Difficulty
from rfq_agent.data_gen.render import MATERIAL_PHRASES
from rfq_agent.data_gen.truth import GroundTruthRow
from rfq_agent.data_gen.world import RETAIL_SITES
from rfq_agent.partno import normalize_part_no

D = Difficulty
OUTLIER_THRESHOLD = 0.40
_WEIGHT = re.compile(r"([\d.]+)\s*(kg|g|lb)\b")
_PHRASE_TO_MATERIAL = {
    phrase.lower(): material for material, phrases in MATERIAL_PHRASES.items() for phrase in phrases
}


def load_truth(dataset_dir: Path) -> list[GroundTruthRow]:
    with (dataset_dir / "ground_truth.jsonl").open(encoding="utf-8") as fh:
        return [GroundTruthRow.model_validate_json(line) for line in fh]


def load_pages(dataset_dir: Path, corpus: str) -> dict[str, dict[str, Any]]:
    """Pages of one corpus keyed by page_id."""
    with (dataset_dir / "pages" / f"{corpus}.jsonl").open(encoding="utf-8") as fh:
        return {rec["page_id"]: rec for rec in map(json.loads, fh)}


def measure(dataset_dir: Path) -> dict[Difficulty, int]:
    """Row counts per section 6.3 difficulty, derived from artifacts."""
    truth = load_truth(dataset_dir)
    catalog = load_pages(dataset_dir, "oem_catalog")
    spec_pages = load_pages(dataset_dir, "external_spec")
    retail = {site: load_pages(dataset_dir, site) for site in RETAIL_SITES}
    internal = _internal_specs(dataset_dir / "internal.db")
    raw_numbers = _raw_customer_numbers(dataset_dir, len(truth))

    primary_counts = Counter(t.primary_oe for t in truth if t.primary_oe)
    counts: Counter[Difficulty] = Counter()
    for t in truth:
        oe = t.primary_oe
        if oe is None:
            counts[D.OE_NOT_FOUND] += 1
        else:
            key = normalize_part_no(oe)
            if primary_counts[oe] > 1:
                counts[D.MANY_TO_ONE] += 1
            elif len(t.accepted_oes) > 1:
                counts[D.ONE_TO_MANY] += 1
            first = catalog.get(f"cat-{normalize_part_no(t.supersession_chain[0])}")
            if first and "superseded" in first["text"].lower() and primary_counts[oe] == 1:
                counts[D.SUPERSESSION] += 1

            pages = [retail[site].get(f"{site}-{key}") for site in RETAIL_SITES]
            present = [p for p in pages if p is not None]
            if primary_counts[oe] == 1:
                if len(present) < len(RETAIL_SITES):
                    counts[D.RETAIL_MISSING] += 1
                if any(oe not in p["title"] for p in present):
                    counts[D.RETAIL_VARIANT_SPELLING] += 1
                if _has_outlier(t):
                    counts[D.RETAIL_OUTLIER] += 1

        own = internal.get(t.our_part_no)
        if (
            t.our_part_reachability != "unreachable"
            and own is not None
            and primary_counts.get(oe or "", 0) <= 1
            and oe is not None
        ):
            material, weight = own
            if material is None or weight is None:
                counts[D.INTERNAL_SPEC_MISSING] += 1
            else:
                page = spec_pages.get(f"spec-{normalize_part_no(oe)}")
                if page is not None and _conflicts(page["text"], material, weight):
                    counts[D.SPEC_CONFLICT] += 1

        if _is_dirty(raw_numbers.get(t.row_id, ""), t.customer_part_no):
            counts[D.DIRTY_INPUT] += 1
    return {d: counts.get(d, 0) for d in SECTION_6_3}


def _has_outlier(t: GroundTruthRow) -> bool:
    prices = [lst.effective_price for lst in t.retail.sites.values() if lst is not None]
    if len(prices) < 3:
        return False
    median = statistics.median(prices)
    return any(abs(p - median) / median > OUTLIER_THRESHOLD for p in prices)


def _internal_specs(path: Path) -> dict[str, tuple[str | None, float | None]]:
    conn = sqlite3.connect(path)
    try:
        return {
            no: (material, weight)
            for no, material, weight in conn.execute(
                "SELECT our_part_no, material, weight_kg FROM our_parts"
            )
        }
    finally:
        conn.close()


def parse_spec_page(text: str) -> tuple[str | None, float | None]:
    """Material key and weight in kg as published on an external spec page."""
    material = None
    lowered = text.lower()
    for phrase in sorted(_PHRASE_TO_MATERIAL, key=len, reverse=True):
        if phrase in lowered:
            material = _PHRASE_TO_MATERIAL[phrase]
            break
    weight = None
    match = _WEIGHT.search(text)
    if match:
        value, unit = float(match.group(1)), match.group(2)
        weight = value if unit == "kg" else value / 1000 if unit == "g" else value / 2.20462
    return material, weight


def _conflicts(text: str, material: str, weight: float) -> bool:
    ext_material, ext_weight = parse_spec_page(text)
    if ext_material is not None and ext_material != material:
        return True
    return ext_weight is not None and abs(ext_weight - weight) / weight > 0.10


def _raw_customer_numbers(dataset_dir: Path, n_rows: int) -> dict[int, str]:
    wb = load_workbook(dataset_dir / f"rfq_{n_rows}.xlsx", read_only=True)
    ws = wb.active
    assert ws is not None
    result: dict[int, str] = {}
    row_id = 0
    for values in ws.iter_rows(min_row=2, values_only=True):
        if all(v is None or str(v).strip() == "" for v in values):
            continue
        row_id += 1
        result[row_id] = str(values[0])
    wb.close()
    return result


def _is_dirty(raw: str, clean: str) -> bool:
    return raw != clean
