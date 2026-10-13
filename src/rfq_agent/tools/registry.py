"""Build the named mock tools (spec section 9) for a generated dataset."""

from __future__ import annotations

from pathlib import Path

from rfq_agent.config import ToolSettings
from rfq_agent.tools.corpus import PageCorpusTool

# Retail site (quote source and corpus name) -> its search tool. The one place that
# pairs the two names.
RETAIL_SITE_TOOLS: dict[str, str] = {f"retail_{i}": f"retail_search_{i}" for i in range(1, 6)}

TOOL_CORPORA: dict[str, str] = {
    "oem_catalog_search": "oem_catalog",
    "interchange_search": "interchange",
    "msrp_lookup": "msrp",
    **{tool: site for site, tool in RETAIL_SITE_TOOLS.items()},
    "external_spec_search": "external_spec",
}


def build_tools(dataset_dir: Path, settings: ToolSettings) -> dict[str, PageCorpusTool]:
    pages_dir = dataset_dir / "pages"
    if not pages_dir.is_dir():
        raise FileNotFoundError(
            f"No generated pages in {pages_dir}. Run `rfq data generate` first."
        )
    return {
        name: PageCorpusTool.from_jsonl(
            name,
            pages_dir / f"{corpus}.jsonl",
            top_k=settings.top_k,
            max_decoys=settings.max_decoys,
            faults=settings.faults,
        )
        for name, corpus in TOOL_CORPORA.items()
    }
