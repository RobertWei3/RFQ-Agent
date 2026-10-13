"""``rfq worksheet build``: entity resolution + confidence over the four agent outputs."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Protocol, TypeVar, get_args

import typer

from rfq_agent.cli.runs import (
    RunFiles,
    announce_and_guard,
    default_truth,
    load_truth_file,
    read_lines,
)
from rfq_agent.config import get_settings, resolve_path
from rfq_agent.schemas import StrategyName

if TYPE_CHECKING:
    from rfq_agent.config import Settings
    from rfq_agent.schemas import ResolvedRow, RFQLine


class _HasRowId(Protocol):
    row_id: int


_Outcome = TypeVar("_Outcome", bound=_HasRowId)

worksheet_app = typer.Typer(help="Build the pricing worksheet.", no_args_is_help=True)

AGENT = "worksheet"


@worksheet_app.command("build")
def worksheet_build(
    oe: Annotated[Path, typer.Option("--oe", help="oe_interchange JSONL.")],
    msrp: Annotated[Path, typer.Option("--msrp", help="oe_msrp JSONL.")],
    retail: Annotated[Path, typer.Option("--retail", help="retail_price JSONL.")],
    cost: Annotated[Path, typer.Option("--cost", help="should_cost JSONL.")],
    rfq: Annotated[
        Path | None, typer.Option("--rfq", help="RFQ workbook (default: recorded upstream).")
    ] = None,
    strategy: Annotated[
        str | None, typer.Option(help="evidence | evidence_plus_self | evidence_plus_judge.")
    ] = None,
    output: Annotated[
        Path | None, typer.Option(help="JSONL output (default data/runs/worksheet_<ts>.jsonl).")
    ] = None,
    limit: Annotated[int | None, typer.Option(help="Only the first N RFQ rows.")] = None,
    truth: Annotated[
        Path | None, typer.Option(help="Ground truth JSONL for the comparison table.")
    ] = None,
    allow_real: Annotated[
        bool, typer.Option("--allow-real", help="Permit real (paid) LLM calls.")
    ] = False,
    trace: Annotated[bool, typer.Option(help="Write a local trace next to the output.")] = True,
) -> None:
    """Merge the four agent outputs per row and score confidence (spec 12.1, 12.2)."""
    from rfq_agent.confidence import score_rows

    settings = get_settings()
    name = parse_strategy(strategy, settings)
    workbook = _workbook(rfq, cost, oe)
    lines = read_lines(workbook, limit)
    rows = _resolve(lines, oe=oe, msrp=msrp, retail=retail, cost=cost, limited=limit is not None)
    _announce(settings, name, rows, allow_real)

    run = RunFiles.create(AGENT, output, trace)
    scored = asyncio.run(score_rows(rows, settings, name, run_id=run.run_id, trace=run.writer))
    _write(scored, run.output)
    bands = Counter(r.worksheet.band for r in scored)
    typer.echo(f"bands: green {bands['green']}, yellow {bands['yellow']}, red {bands['red']}")
    role = _llm_role(name)
    run.finish(
        settings,
        rows=len(scored),
        failed=sum(bool(r.worksheet.failures) for r in scored),
        strategy=name,
        llm_role=role,
        llm_model=settings.llm.for_role(role).model if role else None,
        rfq=str(resolve_path(workbook)),
        inputs={"oe": str(oe), "msrp": str(msrp), "retail": str(retail), "cost": str(cost)},
    )
    truth_path = resolve_path(truth) if truth else default_truth(workbook, settings.dataset_dir)
    if truth_path is not None and truth_path.is_file():
        _report(scored, truth_path, run)


def parse_strategy(strategy: str | None, settings: Settings) -> StrategyName:
    choices: tuple[StrategyName, ...] = get_args(StrategyName)
    if strategy is None:
        return settings.confidence.strategy
    for choice in choices:
        if choice == strategy:
            return choice
    raise typer.BadParameter(f"--strategy must be one of {', '.join(choices)}.")


def _workbook(rfq: Path | None, *upstream: Path) -> Path:
    """--rfq, else the workbook an upstream run recorded in its .meta.json."""
    from rfq_agent.agents.runner import read_meta

    if rfq is not None:
        return rfq
    for path in upstream:
        meta = read_meta(resolve_path(path))
        recorded = meta.get("rfq") or meta.get("input")
        if recorded:
            return Path(recorded)
    raise typer.BadParameter("No upstream run records its RFQ workbook; pass it with --rfq.")


def _resolve(
    lines: list[RFQLine], *, oe: Path, msrp: Path, retail: Path, cost: Path, limited: bool
) -> list[ResolvedRow]:
    from rfq_agent.agents.runner import read_outcomes
    from rfq_agent.resolution import ResolutionInputError, resolve_rows
    from rfq_agent.schemas import MSRPResult, OEResolution, RetailResult, ShouldCostResult

    wanted = {line.row_id for line in lines}

    def keep(outcomes: list[_Outcome]) -> list[_Outcome]:
        # With --limit only the first N RFQ rows are built; each file must still hold them.
        return [o for o in outcomes if o.row_id in wanted] if limited else outcomes

    try:
        return resolve_rows(
            lines,
            keep(read_outcomes(resolve_path(oe), OEResolution)),
            keep(read_outcomes(resolve_path(msrp), MSRPResult)),
            keep(read_outcomes(resolve_path(retail), RetailResult)),
            keep(read_outcomes(resolve_path(cost), ShouldCostResult)),
        )
    except ResolutionInputError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _announce(
    settings: Settings, strategy: StrategyName, rows: Sequence[ResolvedRow], allow_real: bool
) -> None:
    """Print the expected LLM calls; strategies that call an LLM go through the real-cost guard."""
    from rfq_agent.confidence import expected_llm_calls

    expected = expected_llm_calls(rows, settings, strategy)
    calls = (expected, expected * (1 + settings.pipeline.structured_output_retries))
    role = _llm_role(strategy)
    typer.echo(f"strategy={strategy}")
    if role is None:
        typer.echo(f"rows={len(rows)} (no LLM)")
        typer.echo(f"LLM calls: expected ~{calls[0]}, upper bound {calls[1]}")
        return
    announce_and_guard(settings, role, len(rows), calls, allow_real)


def _llm_role(strategy: StrategyName) -> str | None:
    from rfq_agent.confidence.llm_checks import JUDGE_ROLE, SELF_ROLE

    return {"evidence_plus_self": SELF_ROLE, "evidence_plus_judge": JUDGE_ROLE}.get(strategy)


def _write(rows: Sequence[ResolvedRow], path: Path) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(row.model_dump_json() + "\n")


def _report(rows: Sequence[ResolvedRow], truth_path: Path, run: RunFiles) -> None:
    from rfq_agent.eval.quick_compare import compare_worksheet, render_worksheet_markdown

    run.write_report(
        render_worksheet_markdown(compare_worksheet(rows, load_truth_file(truth_path)))
    )
