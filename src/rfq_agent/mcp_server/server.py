"""Internal-database MCP server (stdio), backed by the generated ``internal.db``.

The database path comes from ``$RFQ_INTERNAL_DB`` or, by default,
``<settings.data.dataset_dir>/internal.db``. It is resolved on every call so
tests can point the server at a temporary dataset.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel

from rfq_agent.config import CostModel, get_settings
from rfq_agent.partno import normalize_part_no
from rfq_agent.schemas import MaterialList, MaterialPrice, PartSpec

ENV_INTERNAL_DB = "RFQ_INTERNAL_DB"

mcp = FastMCP(
    "rfq-internal-db",
    instructions="Read-only access to the company's internal parts master, "
    "customer cross-references, material prices and cost-model rates.",
    log_level="WARNING",
)


class OurPartLookup(BaseModel):
    found: bool
    customer_part_no: str
    our_part_no: str | None


def internal_db_path() -> Path:
    override = os.environ.get(ENV_INTERNAL_DB)
    return Path(override) if override else get_settings().dataset_dir / "internal.db"


def _query_all(sql: str, params: tuple[str, ...] = ()) -> list[sqlite3.Row]:
    path = internal_db_path()
    if not path.is_file():
        raise FileNotFoundError(f"Internal DB not found at {path}. Run `rfq data generate`.")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _query(sql: str, params: tuple[str, ...]) -> sqlite3.Row | None:
    rows = _query_all(sql, params)
    return rows[0] if rows else None


@mcp.tool()
def lookup_our_part(customer_part_no: str, customer_name: str | None = None) -> OurPartLookup:
    """Map a customer part number to our part number via the known cross-reference.

    Matching uses the normalized part number (case, spaces and separators ignored).
    ``customer_name`` is accepted for forward compatibility; the dataset has one customer.
    """
    row = _query(
        "SELECT our_part_no FROM customer_xref WHERE customer_part_no_norm = ?",
        (normalize_part_no(customer_part_no),),
    )
    return OurPartLookup(
        found=row is not None,
        customer_part_no=customer_part_no,
        our_part_no=row["our_part_no"] if row else None,
    )


@mcp.tool()
def get_part_spec(our_part_no: str) -> PartSpec:
    """Return category, material, weight (kg) and cycle time (min) for one of our parts.

    Material or weight may be null when the master data is incomplete.
    """
    row = _query("SELECT * FROM our_parts WHERE our_part_no = ?", (our_part_no.strip().upper(),))
    if row is None:
        return PartSpec(found=False, our_part_no=our_part_no)
    return PartSpec(
        found=True,
        our_part_no=row["our_part_no"],
        category=row["category"],
        description=row["description"],
        material=row["material"],
        weight_kg=row["weight_kg"],
        cycle_time_min=row["cycle_time_min"],
    )


@mcp.tool()
def get_material_price(material: str) -> MaterialPrice:
    """Return the current price in USD per kg for a material key such as ``aluminum``."""
    key = material.strip().lower().replace(" ", "_").replace("-", "_")
    row = _query("SELECT * FROM material_prices WHERE material = ?", (key,))
    if row is None:
        return MaterialPrice(found=False, material=material)
    return MaterialPrice(
        found=True,
        material=row["material"],
        price_per_kg=row["price_per_kg"],
        currency=row["currency"],
        as_of=row["as_of"],
    )


@mcp.tool()
def list_materials() -> MaterialList:
    """Return every material key that has a price in the internal database, sorted."""
    rows = _query_all("SELECT material FROM material_prices ORDER BY material")
    return MaterialList(materials=[row["material"] for row in rows])


@mcp.tool()
def get_cost_params() -> CostModel:
    """Return the should-cost and landed-cost rates (illustrative values only)."""
    return get_settings().cost_model


def run_stdio() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    run_stdio()
