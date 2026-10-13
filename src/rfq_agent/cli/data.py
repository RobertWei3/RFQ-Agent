"""``rfq data``, ``rfq tools`` and ``rfq mcp`` commands."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer

from rfq_agent.config import PROJECT_ROOT, get_settings, resolve_path

data_app = typer.Typer(help="Synthetic data and ground truth.", no_args_is_help=True)
tools_app = typer.Typer(help="Mock external tools.", no_args_is_help=True)
mcp_app = typer.Typer(help="Internal-database MCP server.", no_args_is_help=True)

DEFAULT_SAMPLE_PATH = PROJECT_ROOT / "data" / "samples" / "rfq_sample_20.xlsx"


@data_app.command("generate")
def data_generate(
    n_parts: Annotated[int, typer.Option(help="Number of RFQ rows.")] = 200,
    seed: Annotated[int, typer.Option(help="Random seed.")] = 42,
    out: Annotated[
        Path | None, typer.Option(help="Output dir (default data/generated/n{N}_s{seed}).")
    ] = None,
    ratio: Annotated[
        list[str] | None,
        typer.Option(help="Override a difficulty ratio, e.g. --ratio supersession=0.2."),
    ] = None,
    sample_out: Annotated[
        Path | None,
        typer.Option(help="Where to write the 20-row sample (default: data/samples for 200/42)."),
    ] = None,
) -> None:
    """Generate a synthetic dataset with ground truth."""
    from rfq_agent.data_gen.params import Difficulty, GenParams
    from rfq_agent.data_gen.writer import default_dataset_dir, generate_dataset

    ratios = GenParams().ratios
    for item in ratio or []:
        name, _, value = item.partition("=")
        try:
            ratios[Difficulty(name.strip())] = float(value)
        except ValueError as exc:
            raise typer.BadParameter(f"Invalid --ratio {item!r}") from exc
    params = GenParams(n_parts=n_parts, seed=seed, ratios=ratios)

    out_dir = (
        resolve_path(out)
        if out
        else default_dataset_dir(PROJECT_ROOT / "data" / "generated", n_parts, seed)
    )
    if sample_out is None and (n_parts, seed) == (200, 42):
        sample_out = DEFAULT_SAMPLE_PATH
    manifest = generate_dataset(params, get_settings().cost_model, out_dir, sample_out)
    typer.echo(f"Dataset written to {out_dir}")
    if sample_out is not None:
        typer.echo(f"Sample written to {sample_out}")
    _print_stats(manifest, out_dir)


@data_app.command("stats")
def data_stats(
    dataset: Annotated[
        Path | None, typer.Option(help="Dataset dir (default: settings data.dataset_dir).")
    ] = None,
) -> None:
    """Show realized difficulty ratios of a generated dataset."""
    directory = resolve_path(dataset) if dataset else get_settings().dataset_dir
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        typer.echo(f"No manifest at {manifest_path}. Run `rfq data generate`.", err=True)
        raise typer.Exit(code=1)
    _print_stats(json.loads(manifest_path.read_text(encoding="utf-8")), directory)


def _print_stats(manifest: dict[str, object], dataset_dir: Path) -> None:
    """Print target ratios next to ratios measured from the written artifacts."""
    from rfq_agent.data_gen.audit import measure
    from rfq_agent.data_gen.params import Difficulty

    section = manifest["section_6_3"]
    rows = manifest["rows"]
    assert isinstance(section, dict) and isinstance(rows, int)
    measured = measure(dataset_dir)
    typer.echo(f"rows={rows}  (ratios measured from pages, internal DB and workbook)")
    typer.echo(f"{'difficulty':<26}{'target':>8}{'measured':>10}{'ratio':>8}{'diff':>8}")
    for name, item in section.items():
        count = measured[Difficulty(name)]
        ratio = count / rows
        typer.echo(
            f"{name:<26}{item['target']:>8.1%}{count:>10}{ratio:>8.1%}"
            f"{ratio - item['target']:>+8.1%}"
        )
    typer.echo(f"extra tags: {manifest['extra_tags']}")
    typer.echo(f"our part reachability: {manifest['our_part_reachability']}")
    typer.echo(f"corpus sizes: {manifest['corpus_sizes']}")


@tools_app.command("search")
def tools_search(
    tool: Annotated[str, typer.Argument(help="Tool name, e.g. retail_search_3.")],
    query: Annotated[str, typer.Argument(help="Part number to search for.")],
) -> None:
    """Run one mock tool search against the active dataset."""
    from rfq_agent.tools import build_tools

    settings = get_settings()
    tools = build_tools(settings.dataset_dir, settings.tools)
    if tool not in tools:
        raise typer.BadParameter(f"Unknown tool {tool!r}; choose from {sorted(tools)}")
    hits = asyncio.run(tools[tool].search(query))
    if not hits:
        typer.echo("(no results)")
    for hit in hits:
        typer.echo(f"=== {hit.page_id} | {hit.title}\n{hit.text}\n")


@mcp_app.command("serve")
def mcp_serve() -> None:
    """Start the internal-database MCP server on stdio (for agents or MCP Inspector)."""
    from rfq_agent.mcp_server.server import run_stdio

    run_stdio()
