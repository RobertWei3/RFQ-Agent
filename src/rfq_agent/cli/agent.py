"""``rfq agent`` commands: run one subagent over an RFQ and write JSONL."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from rfq_agent.cli.runs import (
    RunFiles,
    announce_and_guard,
    count_failures,
    default_truth,
    guard_real,
    load_truth_file,
    read_lines,
)
from rfq_agent.config import get_settings, resolve_path

if TYPE_CHECKING:
    from rfq_agent.agents.common import RowFailure
    from rfq_agent.agents.should_cost import ShouldCostInput
    from rfq_agent.config import Settings
    from rfq_agent.data_gen.truth import GroundTruthRow
    from rfq_agent.schemas import OEResolution, RFQLine

agent_app = typer.Typer(help="Run individual subagents.", no_args_is_help=True)

OutputOpt = Annotated[
    Path | None, typer.Option(help="JSONL output (default data/runs/<agent>_<ts>.jsonl).")
]
DatasetOpt = Annotated[
    Path | None, typer.Option(help="Dataset dir for tools / MCP (default: settings).")
]
LimitOpt = Annotated[int | None, typer.Option(help="Only process the first N rows.")]
TruthOpt = Annotated[Path | None, typer.Option(help="Ground truth JSONL for the comparison table.")]
AllowRealOpt = Annotated[bool, typer.Option("--allow-real", help="Permit real (paid) LLM calls.")]
TraceOpt = Annotated[
    bool, typer.Option(help="Write a local step-by-step trace next to the output.")
]
FromOeOpt = Annotated[Path | None, typer.Option("--from-oe", help="oe_interchange JSONL to price.")]
RfqOpt = Annotated[
    Path | None,
    typer.Option("--rfq", help="RFQ workbook behind --from-oe (default: the one it recorded)."),
]
InputOpt = Annotated[
    Path | None, typer.Option("--input", help="RFQ workbook; runs oe_interchange first.")
]


@agent_app.command("oe-interchange")
def agent_oe_interchange(
    input: Annotated[Path, typer.Option("--input", help="RFQ workbook (.xlsx).")],
    output: OutputOpt = None,
    dataset: DatasetOpt = None,
    limit: LimitOpt = None,
    truth: TruthOpt = None,
    allow_real: AllowRealOpt = False,
    trace: TraceOpt = True,
) -> None:
    """Resolve customer part numbers to OE numbers (spec section 11.1); writes JSONL."""
    from rfq_agent.agents.runner import run_oe_interchange, write_outcomes
    from rfq_agent.eval.quick_compare import compare, render_markdown

    settings = get_settings()
    lines = read_lines(input, limit)
    announce_and_guard(
        settings, "oe_interchange", len(lines), oe_calls(settings, len(lines)), allow_real
    )
    run = RunFiles.create("oe_interchange", output, trace)
    dataset_dir = resolve_path(dataset) if dataset else settings.dataset_dir

    outcomes: Sequence[Any] = asyncio.run(
        run_oe_interchange(
            lines, settings, dataset_dir=dataset_dir, run_id=run.run_id, trace=run.writer
        )
    )
    write_outcomes(outcomes, run.output)
    run.finish(
        settings, len(outcomes), count_failures(outcomes), input=input, dataset_dir=dataset_dir
    )

    truth_path = resolve_path(truth) if truth else default_truth(input, dataset_dir)
    if truth_path is not None and truth_path.is_file():
        run.write_report(render_markdown(compare(outcomes, load_truth_file(truth_path))))


@agent_app.command("oe-msrp")
def agent_oe_msrp(
    from_oe: FromOeOpt = None,
    input: InputOpt = None,
    output: OutputOpt = None,
    dataset: DatasetOpt = None,
    limit: LimitOpt = None,
    truth: TruthOpt = None,
    allow_real: AllowRealOpt = False,
    trace: TraceOpt = True,
) -> None:
    """Look up the MSRP of each resolved OE number (spec section 11.2); writes JSONL."""
    from rfq_agent.agents.runner import run_oe_msrp
    from rfq_agent.eval.quick_compare import compare_msrp, render_msrp_markdown

    run_downstream_agent(
        DownstreamAgent(
            "oe_msrp",
            run_oe_msrp,
            lambda o, t: render_msrp_markdown(compare_msrp(o, t)),
            oe_outcomes_as_inputs,
        ),
        DownstreamArgs(from_oe, input, output, dataset, limit, truth, allow_real, trace),
    )


@agent_app.command("retail-price")
def agent_retail_price(
    from_oe: FromOeOpt = None,
    input: InputOpt = None,
    output: OutputOpt = None,
    dataset: DatasetOpt = None,
    limit: LimitOpt = None,
    truth: TruthOpt = None,
    allow_real: AllowRealOpt = False,
    trace: TraceOpt = True,
) -> None:
    """Collect retail quotes from five sites for each resolved OE (spec 11.3); writes JSONL."""
    from rfq_agent.agents.runner import run_retail_price
    from rfq_agent.eval.quick_compare import compare_retail, render_retail_markdown

    run_downstream_agent(
        DownstreamAgent(
            "retail_price",
            run_retail_price,
            lambda o, t: render_retail_markdown(compare_retail(o, t)),
            oe_outcomes_as_inputs,
        ),
        DownstreamArgs(from_oe, input, output, dataset, limit, truth, allow_real, trace),
    )


@agent_app.command("should-cost")
def agent_should_cost(
    from_oe: FromOeOpt = None,
    input: InputOpt = None,
    output: OutputOpt = None,
    dataset: DatasetOpt = None,
    limit: LimitOpt = None,
    truth: TruthOpt = None,
    allow_real: AllowRealOpt = False,
    trace: TraceOpt = True,
    rfq: RfqOpt = None,
) -> None:
    """Estimate should-cost and landed cost from our part's spec (spec 11.4); writes JSONL."""
    from rfq_agent.agents.runner import run_should_cost
    from rfq_agent.eval.quick_compare import compare_should_cost, render_should_cost_markdown

    if rfq is not None and input is not None:
        raise typer.BadParameter("--rfq only goes with --from-oe (--input already is the RFQ).")

    run_downstream_agent(
        DownstreamAgent(
            "should_cost",
            run_should_cost,
            lambda o, t: render_should_cost_markdown(compare_should_cost(o, t)),
            paired_with_rfq_lines,
        ),
        DownstreamArgs(from_oe, input, output, dataset, limit, truth, allow_real, trace, rfq),
    )


@dataclass
class DownstreamArgs:
    """Command-line options shared by the agents downstream of oe_interchange."""

    from_oe: Path | None
    input: Path | None
    output: Path | None
    dataset: Path | None
    limit: int | None
    truth: Path | None
    allow_real: bool
    trace: bool
    rfq: Path | None = None  # should_cost with --from-oe: the RFQ workbook to pair with


# Turns the upstream OE outcomes into the runner's inputs. Called before the run so it
# can fail fast (e.g. a missing RFQ workbook); returns the conversion applied afterwards.
InputBuilder = Callable[
    ["UpstreamPlan", int | None, Path | None],
    Callable[[Sequence["OEResolution | RowFailure"]], Sequence[Any]],
]


def oe_outcomes_as_inputs(
    plan: UpstreamPlan, limit: int | None, rfq: Path | None
) -> Callable[[Sequence[OEResolution | RowFailure]], Sequence[Any]]:
    """oe_msrp / retail_price take the OE outcomes as they are."""
    return lambda upstream: upstream


def paired_with_rfq_lines(
    plan: UpstreamPlan, limit: int | None, rfq: Path | None
) -> Callable[[Sequence[OEResolution | RowFailure]], Sequence[Any]]:
    """should_cost takes (RFQ line, OE outcome) pairs."""
    lines = plan.rfq_lines(limit, rfq)
    return lambda upstream: pair_lines(lines, upstream)


@dataclass
class DownstreamAgent:
    """What differs between downstream agents: name, runner, comparison report, inputs."""

    agent: str
    runner: Callable[..., Coroutine[Any, Any, Sequence[Any]]]
    report: Callable[[Sequence[Any], list[GroundTruthRow]], str]
    inputs: InputBuilder


def run_downstream_agent(spec: DownstreamAgent, args: DownstreamArgs) -> None:
    """Shared flow: plan upstream, guard cost, run, write JSONL + meta, compare with truth."""
    from rfq_agent.agents.runner import write_outcomes

    settings = get_settings()
    dataset_dir = resolve_path(args.dataset) if args.dataset else settings.dataset_dir
    plan = plan_upstream(args.from_oe, args.input, args.limit, settings, args.allow_real)
    rfq = resolve_path(args.rfq) if args.rfq else None
    to_inputs = spec.inputs(plan, args.limit, rfq)
    announce_and_guard(settings, spec.agent, plan.n_rows, plan.calls(settings), args.allow_real)

    run = RunFiles.create(spec.agent, args.output, args.trace)
    inputs = to_inputs(plan.resolve(settings, dataset_dir, run))
    outcomes: Sequence[Any] = asyncio.run(
        spec.runner(inputs, settings, dataset_dir=dataset_dir, run_id=run.run_id, trace=run.writer)
    )
    write_outcomes(outcomes, run.output)
    extra = {"rfq": rfq} if rfq else {}
    run.finish(
        settings,
        len(outcomes),
        count_failures(outcomes),
        input=plan.source_input,
        from_oe=args.from_oe,
        dataset_dir=dataset_dir,
        **extra,
    )

    truth_path = plan.truth_path(args.truth, dataset_dir, rfq)
    if truth_path is not None and truth_path.is_file():
        run.write_report(spec.report(outcomes, load_truth_file(truth_path)))
    elif args.truth is None:
        typer.echo("No ground truth found for this input; pass --truth to get a comparison table.")


# --- shared upstream handling for the downstream agents --------------------------------------


def oe_calls(settings: Settings, rows: int) -> tuple[int, int]:
    """(expected, upper bound) LLM calls for oe_interchange."""
    from rfq_agent.agents.oe_interchange import MAX_HOPS

    retries = 1 + settings.pipeline.structured_output_retries
    return rows * 3, rows * (1 + MAX_HOPS) * retries


@dataclass
class UpstreamPlan:
    """Where a downstream agent gets OE results: a previous JSONL or a fresh oe_interchange run."""

    source_input: Path | None
    n_rows: int
    priced: int  # rows expected to need one LLM call in the downstream agent
    oe_calls: tuple[int, int] = (0, 0)
    lines: list[RFQLine] = field(default_factory=list)
    upstream: list[OEResolution | RowFailure] = field(default_factory=list)

    @property
    def runs_oe(self) -> bool:
        return bool(self.lines)

    def calls(self, settings: Settings) -> tuple[int, int]:
        retries = 1 + settings.pipeline.structured_output_retries
        return (self.oe_calls[0] + self.priced, self.oe_calls[1] + self.priced * retries)

    def resolve(
        self, settings: Settings, dataset_dir: Path, run: RunFiles
    ) -> list[OEResolution | RowFailure]:
        """Return the upstream OE outcomes, running oe_interchange first if needed."""
        if not self.runs_oe:
            return self.upstream
        from rfq_agent.agents.runner import run_oe_interchange, write_outcomes

        upstream = asyncio.run(
            run_oe_interchange(
                self.lines, settings, dataset_dir=dataset_dir, run_id=run.run_id, trace=run.writer
            )
        )
        write_outcomes(upstream, run.output.with_suffix(".oe.jsonl"))
        return upstream

    def rfq_lines(self, limit: int | None, rfq: Path | None) -> list[RFQLine]:
        """The RFQ lines behind the upstream rows: --input, --rfq, or the recorded workbook."""
        if self.lines:
            return self.lines
        workbook = rfq or self.source_input
        if workbook is None:
            raise typer.BadParameter(
                "The --from-oe run does not record its RFQ workbook (no .meta.json); "
                "pass it with --rfq, or use --input instead."
            )
        return read_lines(workbook, limit)

    def truth_path(
        self, truth: Path | None, dataset_dir: Path, rfq: Path | None = None
    ) -> Path | None:
        if truth:
            return resolve_path(truth)
        workbook = self.source_input or rfq
        return default_truth(workbook, dataset_dir) if workbook else None


def pair_lines(
    lines: Sequence[RFQLine], upstream: Sequence[OEResolution | RowFailure]
) -> list[ShouldCostInput]:
    """Match each OE outcome with its RFQ line by row_id."""
    from rfq_agent.agents.should_cost import ShouldCostInput

    by_row = {line.row_id: line for line in lines}
    missing = sorted(o.row_id for o in upstream if o.row_id not in by_row)
    if missing:
        raise typer.BadParameter(f"No RFQ line for upstream row(s) {missing[:5]}.")
    return [ShouldCostInput(line=by_row[o.row_id], upstream=o) for o in upstream]


def plan_upstream(
    from_oe: Path | None,
    input: Path | None,
    limit: int | None,
    settings: Settings,
    allow_real: bool,
) -> UpstreamPlan:
    """Validate --from-oe / --input; with --input also guard the oe_interchange role."""
    from rfq_agent.agents.runner import read_meta, read_oe_outcomes
    from rfq_agent.schemas import OEResolution

    if (from_oe is None) == (input is None):
        raise typer.BadParameter("Pass exactly one of --from-oe or --input.")
    if from_oe is not None:
        upstream = read_oe_outcomes(resolve_path(from_oe))
        upstream = upstream[:limit] if limit else upstream
        recorded = read_meta(resolve_path(from_oe)).get("input")
        # Only rows with a unique OE are priced; the rest short-circuit without an LLM call.
        priced = sum(isinstance(o, OEResolution) and o.status == "resolved" for o in upstream)
        return UpstreamPlan(
            source_input=Path(recorded) if recorded else None,
            n_rows=len(upstream),
            priced=priced,
            upstream=upstream,
        )
    assert input is not None
    lines = read_lines(input, limit)
    guard_real(settings, ["oe_interchange"], allow_real)
    return UpstreamPlan(
        source_input=input,
        n_rows=len(lines),
        priced=len(lines),
        oe_calls=oe_calls(settings, len(lines)),
        lines=lines,
    )
