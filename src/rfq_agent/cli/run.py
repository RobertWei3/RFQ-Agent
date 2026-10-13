"""``rfq run``, ``rfq rerun-failed``, ``rfq runs show``: orchestrated runs (spec 13)."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Callable, Coroutine, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from rfq_agent.cli.runs import default_truth, guard_real, load_truth_file, print_usage, read_lines
from rfq_agent.config import PROJECT_ROOT, get_settings, resolve_path

if TYPE_CHECKING:
    from rfq_agent.config import Settings
    from rfq_agent.orchestrator.run import RunOptions, RunReport
    from rfq_agent.schemas import (
        ResolvedRow,
        ReviewInput,
        ReviewPrompt,
        RFQLine,
        RunSpec,
        StrategyName,
    )

runs_app = typer.Typer(help="Inspect orchestrated runs.", no_args_is_help=True)

OutputDirOpt = Annotated[
    Path | None, typer.Option(help="Where run_<id>.jsonl and its trace go (default data/runs).")
]
AllowRealOpt = Annotated[bool, typer.Option("--allow-real", help="Permit real (paid) LLM calls.")]
TraceOpt = Annotated[bool, typer.Option(help="Append a local trace next to the export.")]
ReviewOpt = Annotated[bool, typer.Option("--review", help="Review red rows interactively.")]

LEASE_EXIT = 3  # another process drives the run, or took it over midway (spec 13 lease)

# resume_run / rerun_failed: (settings, run_id, options) -> report
RunAction = Callable[["Settings", str, "RunOptions"], Coroutine[Any, Any, "RunReport"]]


def run_command(
    input: Annotated[Path | None, typer.Option("--input", help="RFQ workbook (.xlsx).")] = None,
    resume: Annotated[
        str | None, typer.Option("--resume", help="Continue RUN_ID from where it stopped.")
    ] = None,
    review: ReviewOpt = False,
    strategy: Annotated[
        str | None, typer.Option(help="evidence | evidence_plus_self | evidence_plus_judge.")
    ] = None,
    batch_size: Annotated[int | None, typer.Option(min=1, help="Rows per batch.")] = None,
    limit: Annotated[int | None, typer.Option(help="Only the first N RFQ rows.")] = None,
    dataset: Annotated[Path | None, typer.Option(help="Dataset dir (default: settings).")] = None,
    allow_real: AllowRealOpt = False,
    trace: TraceOpt = True,
    output_dir: OutputDirOpt = None,
    lease_token: Annotated[
        str | None, typer.Option(hidden=True, help="Run lease handed over by the API.")
    ] = None,
    worker: Annotated[
        bool, typer.Option(hidden=True, help="Started by the API: review goes through the API.")
    ] = False,
) -> None:
    """Run the whole pipeline over an RFQ in batches, or resume a run (spec 13)."""
    from rfq_agent.orchestrator.run import resume_run

    settings = get_settings()
    if (input is None) == (resume is None):
        raise typer.BadParameter("Pass exactly one of --input or --resume.")
    with _driving(settings, resume, lease_token):
        if resume is not None:
            spec = _spec_of(settings, resume)
            open_rows = _rows_in_open_batches(settings, resume)
            announce_run(settings, spec.strategy, open_rows, allow_real)
            run_id = resume
        else:
            assert input is not None
            lines = read_lines(input, limit)
            spec = _new_spec(settings, input, review, strategy, batch_size, dataset)
            announce_run(settings, spec.strategy, len(lines), allow_real)
            run_id = _create(settings, spec, lines)
        options = _Options(output_dir, trace, lease_token, prompt=not worker)
        _execute(settings, run_id, spec, resume_run, options)


def rerun_failed_command(
    run_id: Annotated[str, typer.Argument(help="Run to repair.")],
    allow_real: AllowRealOpt = False,
    trace: TraceOpt = True,
    output_dir: OutputDirOpt = None,
) -> None:
    """Rerun failed batches and rows with a failure, then finalize again (spec 13)."""
    from rfq_agent.orchestrator.run import rerun_failed
    from rfq_agent.store.runs import open_store

    settings = get_settings()
    spec = _spec_of(settings, run_id)
    with open_store(settings.db_path) as store:
        plan = store.rows_to_rerun(run_id)
    if not plan:
        typer.echo(f"run_id={run_id}: nothing to rerun")
        return
    announce_run(settings, spec.strategy, sum(len(r) for r in plan.values()), allow_real)
    with _driving(settings, run_id, None):
        _execute(settings, run_id, spec, rerun_failed, _Options(output_dir, trace))


def rescore_command(
    run_id: Annotated[str, typer.Argument(help="Finalized run whose stage outcomes to reuse.")],
    strategy: Annotated[
        str, typer.Option(help="evidence | evidence_plus_self | evidence_plus_judge.")
    ],
    allow_real: AllowRealOpt = False,
    trace: TraceOpt = True,
    output_dir: OutputDirOpt = None,
) -> None:
    """New run with RUN_ID's stage outcomes, finalized with another confidence strategy.

    No stage agent runs again (spec 13 rescore), so strategies compare on the same results.
    """
    from rfq_agent.cli.worksheet import parse_strategy
    from rfq_agent.store.runs import RescoreRefused, open_store

    settings = get_settings()
    name = parse_strategy(strategy, settings)
    with open_store(settings.db_path) as store:
        try:
            store.check_rescorable(run_id)  # refuse before announcing any calls
        except KeyError as exc:
            raise typer.BadParameter(f"Unknown run {run_id!r}.") from exc
        except RescoreRefused as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc
        announce_rescore(settings, name, store.rows(run_id), allow_real)
        new_id = store.copy_for_rescore(run_id, name)
        spec = store.run(new_id).spec
    typer.echo(f"re-scoring {run_id} with {name}")
    drive_copy(settings, new_id, spec, output_dir, trace)


def drive_copy(
    settings: Settings, run_id: str, spec: RunSpec, output_dir: Path | None, trace: bool
) -> None:
    """Finalize a copied run (rescore, injection): its batches are done, so only finalize runs."""
    from rfq_agent.orchestrator.run import resume_run

    with _driving(settings, run_id, None):
        _execute(settings, run_id, spec, resume_run, _Options(output_dir, trace))


@dataclass
class _Options:
    """How the CLI drives a run: outputs, trace, lease handed over, interactive review."""

    output_dir: Path | None
    trace: bool
    lease_token: str | None = None
    prompt: bool = True  # False for an API worker: red rows wait for review via the API


def _execute(
    settings: Settings, run_id: str, spec: RunSpec, action: RunAction, opts: _Options
) -> None:
    """Run ``action`` with the CLI reviewer and trace file, then report and export."""
    from rfq_agent.orchestrator.run import RunOptions

    typer.echo(f"run_id={run_id}  (resume with: rfq run --resume {run_id})")
    out = _output_dir(opts.output_dir)
    options = RunOptions(
        reviewer=cli_reviewer if spec.review and opts.prompt else None,
        trace_path=out / f"run_{run_id}.trace.jsonl" if opts.trace else None,
        lease_token=opts.lease_token,
        # Finalized before the first question: the files are there even if review stops.
        on_finalized=lambda: _export(settings, run_id, out),
    )
    report = asyncio.run(action(settings, run_id, options))
    finish_run(settings, report, out)


@contextmanager
def _driving(settings: Settings, run_id: str | None, lease_token: str | None) -> Iterator[None]:
    """Lease errors exit with ``LEASE_EXIT``; a lease handed over by the API is released.

    Releasing only takes effect if the worker never took the lease over (it then holds it
    under its own token), so a worker failing early does not block the run until expiry.
    """
    from rfq_agent.orchestrator.lease import LeaseLost
    from rfq_agent.store.runs import LeaseHeld, open_store

    try:
        yield
    except (LeaseHeld, LeaseLost) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=LEASE_EXIT) from exc
    finally:
        if run_id is not None and lease_token is not None:
            with open_store(settings.db_path) as store:
                store.release_lease(run_id, lease_token)


@runs_app.command("show")
def runs_show(run_id: Annotated[str, typer.Argument(help="Run to show.")]) -> None:
    """Batch status and executions, bands and LLM usage of one run."""
    from rfq_agent.store.runs import open_store

    settings = get_settings()
    with open_store(settings.db_path) as store:
        record = store.run(run_id)
        typer.echo(
            f"run {run_id}  status {record.status}  input {record.spec.input}  "
            f"batch size {record.spec.batch_size}  strategy {record.spec.strategy}  "
            f"review {record.spec.review}"
        )
        for b in store.batches(run_id):
            error = f"  error {b.error}" if b.error and b.status == "failed" else ""
            typer.echo(
                f"batch {b.batch_id}  rows {b.row_ids[0]}-{b.row_ids[-1]}  {b.status}  "
                f"attempts {b.attempts}{error}"
            )
        _echo_rows(store.rows(run_id))
    print_usage(settings.db_path, run_id)


@runs_app.command("answer", hidden=True)
def runs_answer(
    run_id: Annotated[str, typer.Argument(help="Run awaiting review.")],
    decision: Annotated[str, typer.Option(help="ReviewDecision as JSON.")],
    lease_token: Annotated[
        str | None, typer.Option(help="Run lease handed over by the API.")
    ] = None,
    allow_real: AllowRealOpt = False,
) -> None:
    """Worker for an API review that reruns agents (a new OE number, spec 15.1)."""
    from rfq_agent.orchestrator.estimate import review_calls
    from rfq_agent.orchestrator.run import RunOptions, answer_review
    from rfq_agent.schemas import ReviewDecision

    settings = get_settings()
    with _driving(settings, run_id, lease_token):
        spec = _spec_of(settings, run_id)
        guard_real(settings, review_calls(settings, spec.strategy), allow_real)
        answer = ReviewDecision.model_validate_json(decision)
        options = RunOptions(lease_token=lease_token)
        report = asyncio.run(answer_review(settings, run_id, answer, options))
    typer.echo(f"run_id={run_id} row {answer.row_id} reviewed; run {report.status}")


# --- cost guard ---------------------------------------------------------------------------


def announce_run(settings: Settings, strategy: StrategyName, rows: int, allow_real: bool) -> None:
    """Expected / upper-bound LLM calls per role; refuse real providers without --allow-real."""
    from rfq_agent.orchestrator.estimate import run_calls

    for role, (expected, upper) in run_calls(settings, strategy, rows).items():
        cfg = settings.llm.for_role(role)
        typer.echo(
            f"LLM calls: {role} ({cfg.provider}/{cfg.model}) expected ~{expected}, "
            f"upper bound {upper}"
        )
    typer.echo(f"rows={rows} strategy={strategy}")
    guard_real(settings, run_calls(settings, strategy, rows), allow_real)


def announce_rescore(
    settings: Settings, strategy: StrategyName, rows: list[ResolvedRow], allow_real: bool
) -> None:
    """Exact confidence calls of re-scoring ``rows``; refuse real providers without --allow-real."""
    from rfq_agent.orchestrator.estimate import rescore_calls

    calls = rescore_calls(settings, strategy, rows)
    for role, (expected, upper) in calls.items():
        cfg = settings.llm.for_role(role)
        typer.echo(
            f"LLM calls: {role} ({cfg.provider}/{cfg.model}) expected {expected}, "
            f"upper bound {upper}"
        )
    typer.echo(f"rows={len(rows)} strategy={strategy} (stage agents reused, not run)")
    guard_real(settings, calls, allow_real)


# --- interactive review -------------------------------------------------------------------

CHOICES = "[a]ccept  [o] change OE  [p] change price  [r]eject  [q]uit (resume later)"


async def cli_reviewer(prompt: ReviewPrompt) -> ReviewInput | None:
    """Show one red row and ask for a decision; ``None`` pauses the review."""
    from rfq_agent.schemas import ReviewInput

    typer.echo(render_review(prompt.row, prompt.remaining))
    while True:
        choice = typer.prompt(CHOICES, default="a").strip().lower()[:1]
        if choice == "q":
            return None
        fields = _ask(choice, prompt.row)
        if fields is None:
            continue
        try:
            return ReviewInput.model_validate(fields)
        except ValueError as exc:
            typer.echo(f"invalid: {exc}")


def _ask(choice: str, row: ResolvedRow) -> dict[str, Any] | None:
    if choice == "a":
        return {"action": "accept"}
    if choice == "r":
        return {"action": "reject", "note": typer.prompt("note", default="") or None}
    if choice == "o":
        candidates = row.worksheet.oe_candidates
        for i, oe in enumerate(candidates, 1):
            typer.echo(f"  {i}. {oe}")
        answer = typer.prompt("OE number (or candidate #)").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(candidates):
            answer = candidates[int(answer) - 1]
        return {"action": "modify_oe", "oe": answer}
    if choice == "p":
        msrp = typer.prompt("MSRP (USD, blank = keep)", default="", show_default=False)
        retail = typer.prompt("retail price (USD, blank = keep)", default="", show_default=False)
        return {"action": "modify_price", "msrp": msrp or None, "retail": retail or None}
    typer.echo("choose one of a, o, p, r, q")
    return None


def render_review(row: ResolvedRow, remaining: int) -> str:
    """Evidence and conclusions of one row, for the reviewer."""
    w = row.worksheet
    lines = [
        "",
        f"=== row {w.row_id}  ({remaining} left)  score {w.confidence_score}  band {w.band}",
        f"customer part {w.customer_part_no_raw!r}  our part {w.our_part_no}",
        f"OE {w.oe_status}  current {w.current_oe}  chain {' -> '.join(w.supersession_chain)}",
    ]
    if w.oe_candidates:
        lines.append(f"OE candidates: {', '.join(w.oe_candidates)}")
    lines.append(f"MSRP {w.msrp} ({w.msrp_status})")
    sites = ", ".join(
        f"{site} {price}{' (outlier)' if site in w.retail_outliers else ''}"
        for site, price in w.retail_prices.items()
    )
    lines.append(f"retail: {sites or 'none'}")
    lines.append(f"reference retail {w.selected_retail_price} ({w.selected_retail_source})")
    lines.append(f"should-cost FOB {w.should_cost_fob}  landed {w.landed_cost}")
    if row.confidence is not None:
        lines.append(f"why: {row.confidence.explanation}")
    lines.extend(f"- {reason}" for reason in w.review_reasons)
    lines.extend(f"! {failure}" for failure in w.failures)
    return "\n".join(lines)


# --- helpers ------------------------------------------------------------------------------


def finish_run(settings: Settings, report: RunReport, out: Path) -> None:
    """Print a summary; once the run is finalized, export the rows and compare with truth."""
    from rfq_agent.store.runs import open_store

    with open_store(settings.db_path) as store:
        spec = store.run(report.run_id).spec
        rows = store.rows(report.run_id)
    statuses = Counter(b.status for b in report.batches)
    typer.echo("batches: " + ", ".join(f"{s} {n}" for s, n in sorted(statuses.items())))
    _echo_rows(rows)
    print_usage(settings.db_path, report.run_id)
    if report.status == "running":
        typer.echo(f"running; continue with: rfq run --resume {report.run_id}")
        return
    # Finalized: the worksheet is ready even while red rows await review (spec 13).
    export = _export(settings, report.run_id, out)
    _compare(rows, spec, export)
    if report.status == "awaiting_review":
        typer.echo(f"review paused; continue with: rfq run --resume {report.run_id}")


def _export(settings: Settings, run_id: str, out: Path) -> Path:
    """Rows (JSONL), pricing worksheet (xlsx) and meta of a finalized run."""
    from rfq_agent.agents.runner import write_meta
    from rfq_agent.store.runs import open_store

    with open_store(settings.db_path) as store:
        record = store.run(run_id)
        rows = store.rows(run_id)
    export = out / f"run_{run_id}.jsonl"
    with export.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(row.model_dump_json() + "\n")
    _write_workbook(settings, run_id, export.with_suffix(".xlsx"))
    write_meta(export, run_id=run_id, status=record.status, **record.spec.model_dump())
    typer.echo(f"rows: {export}")
    return export


def _write_workbook(settings: Settings, run_id: str, path: Path) -> None:
    """The pricing worksheet (spec 12.3) from the stored rows."""
    from rfq_agent.output.excel import build_workbook
    from rfq_agent.output.summary import run_summary
    from rfq_agent.store.runs import open_store

    with open_store(settings.db_path) as store:
        summary = run_summary(store, run_id)
        assert summary is not None, "exported only once finalized"
        book = build_workbook(store.run(run_id), store.rows(run_id), summary)
    book.save(path)
    typer.echo(f"worksheet: {path}")


def _compare(rows: list[ResolvedRow], spec: RunSpec, export: Path) -> None:
    """Comparison table against ground truth, when the input has one."""
    from rfq_agent.eval.quick_compare import compare_worksheet, render_worksheet_markdown

    truth = default_truth(Path(spec.input), Path(spec.dataset_dir))
    if truth is None or not truth.is_file() or not rows:
        return
    report_md = render_worksheet_markdown(compare_worksheet(rows, load_truth_file(truth)))
    export.with_suffix(".compare.md").write_text(report_md + "\n", encoding="utf-8")
    typer.echo(report_md.splitlines()[-1])


def _echo_rows(rows: list[ResolvedRow]) -> None:
    bands = Counter(r.worksheet.band for r in rows)
    reviewed = Counter(r.worksheet.review_status for r in rows if r.worksheet.review_status)
    typer.echo(
        f"rows {len(rows)}: green {bands['green']}, yellow {bands['yellow']}, red {bands['red']}"
        + (f"; reviewed {dict(reviewed)}" if reviewed else "")
    )


def _new_spec(
    settings: Settings,
    input: Path,
    review: bool,
    strategy: str | None,
    batch_size: int | None,
    dataset: Path | None,
) -> RunSpec:
    from rfq_agent.cli.worksheet import parse_strategy
    from rfq_agent.schemas import RunSpec

    return RunSpec(
        input=str(resolve_path(input)),
        dataset_dir=str(resolve_path(dataset) if dataset else settings.dataset_dir),
        batch_size=batch_size or settings.pipeline.batch_size,
        strategy=parse_strategy(strategy, settings),
        review=review,
    )


def _create(settings: Settings, spec: RunSpec, lines: list[RFQLine]) -> str:
    from rfq_agent.store.runs import open_store

    with open_store(settings.db_path) as store:
        return store.create_run(spec, lines)


def _spec_of(settings: Settings, run_id: str) -> RunSpec:
    from rfq_agent.store.runs import open_store

    with open_store(settings.db_path) as store:
        try:
            return store.run(run_id).spec
        except KeyError as exc:
            raise typer.BadParameter(f"Unknown run {run_id!r}.") from exc


def _rows_in_open_batches(settings: Settings, run_id: str) -> int:
    """Rows a resume will run: a pending rerun's rows, else whole open batches."""
    from rfq_agent.schemas import OPEN_BATCH_STATUSES
    from rfq_agent.store.runs import open_store

    with open_store(settings.db_path) as store:
        return sum(
            len(b.rows_to_run()) for b in store.batches(run_id) if b.status in OPEN_BATCH_STATUSES
        )


def _output_dir(output_dir: Path | None) -> Path:
    out = resolve_path(output_dir) if output_dir else PROJECT_ROOT / "data" / "runs"
    out.mkdir(parents=True, exist_ok=True)
    return out
