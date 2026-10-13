"""Write a generated world to disk: internal DB, page corpora, ground truth, RFQ workbooks."""

from __future__ import annotations

import json
import random
import sqlite3
from collections import Counter
from dataclasses import asdict
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from openpyxl import Workbook
from openpyxl.writer.excel import ExcelWriter

from rfq_agent.config import CostModel
from rfq_agent.data_gen.params import SECTION_6_3, Difficulty, GenParams
from rfq_agent.data_gen.render import Page, render_corpora
from rfq_agent.data_gen.truth import (
    GroundTruthRow,
    TruthInternalSpec,
    TruthMSRP,
    TruthRetail,
    TruthSpec,
)
from rfq_agent.data_gen.world import RETAIL_SITES, Row, World, build_world
from rfq_agent.partno import normalize_part_no

RFQ_HEADERS = ("Customer Part No", "Our Part No", "Description", "Annual Qty", "Customer Name")
SAMPLE_SIZE = 20
_FIXED_TIMESTAMP = datetime(2026, 1, 1)

INTERNAL_DDL = """
CREATE TABLE our_parts (
    our_part_no    TEXT PRIMARY KEY,
    description    TEXT NOT NULL,
    category       TEXT NOT NULL,
    material       TEXT,
    weight_kg      REAL,
    cycle_time_min REAL NOT NULL
);
CREATE TABLE customer_xref (
    customer_name         TEXT NOT NULL,
    customer_part_no      TEXT NOT NULL,
    customer_part_no_norm TEXT NOT NULL PRIMARY KEY,
    our_part_no           TEXT NOT NULL REFERENCES our_parts (our_part_no)
);
CREATE TABLE material_prices (
    material     TEXT PRIMARY KEY,
    price_per_kg REAL NOT NULL,
    currency     TEXT NOT NULL,
    as_of        TEXT NOT NULL
);
"""


def default_dataset_dir(root: Path, n_parts: int, seed: int) -> Path:
    return root / f"n{n_parts}_s{seed}"


def generate_dataset(
    params: GenParams,
    cost_model: CostModel,
    out_dir: Path,
    sample_out: Path | None = None,
) -> dict[str, Any]:
    """Generate everything into ``out_dir`` and return the manifest."""
    world = build_world(params, cost_model)
    out_dir.mkdir(parents=True, exist_ok=True)

    _write_internal_db(world, out_dir / "internal.db")
    corpora = render_corpora(world)
    pages_dir = out_dir / "pages"
    pages_dir.mkdir(exist_ok=True)
    for name, pages in corpora.items():
        _write_jsonl(pages_dir / f"{name}.jsonl", [_page_dict(p) for p in pages])

    truth = [_truth_row(row) for row in world.rows]
    _write_jsonl(out_dir / "ground_truth.jsonl", [t.model_dump(mode="json") for t in truth])

    blank_rng = random.Random(f"{params.seed}-blank")
    n_blank = max(1, round(params.n_parts * 0.01))
    _write_rfq(
        out_dir / f"rfq_{params.n_parts}.xlsx",
        world.rows,
        params.customer_name,
        blank_rows=n_blank,
        rng=blank_rng,
    )

    sample_rows = select_sample(world.rows, params.seed)
    sample_truth = [
        _truth_row(row).model_copy(update={"row_id": i, "source_row_id": row.row_id})
        for i, row in enumerate(sample_rows, start=1)
    ]
    sample_name = f"rfq_sample_{SAMPLE_SIZE}"
    _write_rfq(
        out_dir / f"{sample_name}.xlsx",
        sample_rows,
        params.customer_name,
        blank_rows=0,
        rng=blank_rng,
    )
    _write_jsonl(
        out_dir / f"{sample_name}_ground_truth.jsonl",
        [t.model_dump(mode="json") for t in sample_truth],
    )
    if sample_out is not None:
        sample_out.parent.mkdir(parents=True, exist_ok=True)
        _write_rfq(sample_out, sample_rows, params.customer_name, blank_rows=0, rng=blank_rng)

    manifest = _manifest(params, world, corpora, n_blank)
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


# --- internal DB ------------------------------------------------------------


def _write_internal_db(world: World, path: Path) -> None:
    path.unlink(missing_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(INTERNAL_DDL)
        parts = {e.our_part.our_part_no: e.our_part for e in world.entities}
        parts.update({p.our_part_no: p for p in world.distractors})
        conn.executemany(
            "INSERT INTO our_parts VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    p.our_part_no,
                    p.description,
                    p.spec.category,
                    p.internal_material,
                    p.internal_weight_kg,
                    p.spec.cycle_time_min,
                )
                for p in sorted(parts.values(), key=lambda p: p.our_part_no)
            ],
        )
        conn.executemany(
            "INSERT INTO customer_xref VALUES (?, ?, ?, ?)",
            [
                (
                    world.params.customer_name,
                    row.customer_part_no,
                    normalize_part_no(row.customer_part_no),
                    row.entity.our_part.our_part_no,
                )
                for row in world.rows
                if row.reachability == "xref"
            ],
        )
        conn.executemany(
            "INSERT INTO material_prices VALUES (?, ?, 'USD', '2026-01-01')",
            sorted(world.material_prices.items()),
        )
        conn.commit()
    finally:
        conn.close()


# --- ground truth -----------------------------------------------------------


def _truth_row(row: Row) -> GroundTruthRow:
    entity = row.entity
    primary = entity.primary
    prices = entity.prices
    spec = entity.our_part.spec

    if primary is None:
        status = "not_found"
    elif entity.others:
        status = "multiple"
    else:
        status = "resolved"

    if prices is None:
        msrp = TruthMSRP(status="not_found", value=None, source_oe=None)
        retail = TruthRetail(
            sites=dict.fromkeys(RETAIL_SITES), outlier_sites=[], expected_selected_site=None
        )
    else:
        msrp = TruthMSRP(
            status=prices.msrp_status,
            value=None if prices.msrp_status == "not_found" else prices.msrp,
            source_oe=prices.msrp_source_oe,
        )
        retail = TruthRetail(
            sites=dict(prices.listings),
            outlier_sites=list(prices.outlier_sites),
            expected_selected_site=prices.expected_selected_site,
        )

    return GroundTruthRow(
        row_id=row.row_id,
        customer_part_no=row.customer_part_no,
        customer_part_no_raw=row.customer_part_no_raw,
        tags=sorted(t.value for t in row.tags),
        our_part_no=entity.our_part.our_part_no,
        our_part_reachability=row.reachability,
        oe_status_expected=status,
        primary_oe=primary.oe_no if primary else None,
        accepted_oes=[p.oe_no for p in ([primary] if primary else []) + entity.others],
        supersession_chain=list(primary.chain) if primary else [],
        shares_oe_with=list(row.group),
        msrp=msrp,
        retail=retail,
        spec=TruthSpec(
            category=spec.category,
            material=spec.material,
            weight_kg=spec.weight_kg,
            cycle_time_min=spec.cycle_time_min,
        ),
        internal_spec=TruthInternalSpec(
            material=entity.our_part.internal_material,
            weight_kg=entity.our_part.internal_weight_kg,
        ),
        external_spec_available=entity.external_spec is not None,
        cost=entity.cost,
    )


# --- workbooks --------------------------------------------------------------


def _write_rfq(
    path: Path, rows: list[Row], customer_name: str, *, blank_rows: int, rng: random.Random
) -> None:
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "RFQ"
    ws.append(list(RFQ_HEADERS))
    blank_after = set(rng.sample(range(len(rows)), min(blank_rows, len(rows))))
    for i, row in enumerate(rows):
        ws.append(
            [
                row.customer_part_no_raw,
                row.entity.our_part.our_part_no if row.reachability == "given" else None,
                row.description,
                row.annual_qty,
                _customer_name(row, customer_name),
            ]
        )
        if i in blank_after:
            ws.append([None] * len(RFQ_HEADERS))
    wb.properties.created = _FIXED_TIMESTAMP
    wb.properties.modified = _FIXED_TIMESTAMP
    _save_reproducibly(wb, path)


def _save_reproducibly(wb: Workbook, path: Path) -> None:
    """Save so that equal content gives equal bytes.

    ``Workbook.save`` stamps the save time into the document properties and every zip entry,
    which would dirty the committed sample on each ``rfq data generate``.
    """
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        ExcelWriter(wb, archive).write_data()
    with ZipFile(buffer) as source, ZipFile(path, "w", ZIP_DEFLATED) as target:
        for item in source.infolist():
            entry = ZipInfo(item.filename, date_time=_FIXED_TIMESTAMP.timetuple()[:6])
            entry.compress_type = ZIP_DEFLATED
            entry.external_attr = item.external_attr
            target.writestr(entry, source.read(item))


def _customer_name(row: Row, name: str) -> str:
    return f" {name}  " if Difficulty.DIRTY_INPUT in row.tags else name


def select_sample(rows: list[Row], seed: int, size: int = SAMPLE_SIZE) -> list[Row]:
    """Stratified sample: every difficulty at least once, many-to-one groups kept whole."""
    rng = random.Random(f"{seed}-sample")
    order = list(rows)
    rng.shuffle(order)
    chosen: dict[int, Row] = {}
    by_id = {r.row_id: r for r in rows}

    def add(row: Row) -> None:
        chosen[row.row_id] = row
        for other in row.group:
            chosen[other] = by_id[other]

    for tag in Difficulty:
        if any(tag in r.tags for r in chosen.values()):
            continue
        candidate = next((r for r in order if tag in r.tags and r.row_id not in chosen), None)
        if candidate is not None:
            add(candidate)
    for row in order:
        if len(chosen) >= size:
            break
        if not row.group and row.row_id not in chosen:
            add(row)
    return sorted(chosen.values(), key=lambda r: r.row_id)


# --- helpers ----------------------------------------------------------------


def _page_dict(page: Page) -> dict[str, Any]:
    data = asdict(page)
    data["keys"] = list(page.keys)
    return data


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _manifest(
    params: GenParams, world: World, corpora: dict[str, list[Page]], n_blank: int
) -> dict[str, Any]:
    n = params.n_parts
    counts = Counter(t.value for row in world.rows for t in row.tags)
    reach = Counter(row.reachability for row in world.rows)
    return {
        "generator_version": 1,
        "params": params.model_dump(mode="json"),
        "rows": n,
        "blank_rows_in_rfq": n_blank,
        "section_6_3": {
            d.value: {
                "target": params.ratios.get(d, 0.0),
                "count": counts.get(d.value, 0),
                "realized": counts.get(d.value, 0) / n,
            }
            for d in SECTION_6_3
        },
        "extra_tags": {d.value: counts.get(d.value, 0) for d in Difficulty if d not in SECTION_6_3},
        "our_part_reachability": dict(sorted(reach.items())),
        "corpus_sizes": {name: len(pages) for name, pages in sorted(corpora.items())},
    }
