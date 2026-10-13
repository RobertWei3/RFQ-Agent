"""``rfq eval --run`` and ``rfq eval compare``: evaluation against ground truth (spec 14)."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from rfq_agent.cli.run import OutputDirOpt, TraceOpt, drive_copy
from rfq_agent.config import PROJECT_ROOT, get_settings, resolve_path

if TYPE_CHECKING:
    from rfq_agent.schemas import RunEval

eval_app = typer.Typer(
    help="Evaluate runs against ground truth (spec 14).", invoke_without_command=True
)

OutOpt = Annotated[Path | None, typer.Option("--out", help="Report directory (default reports).")]


@eval_app.callback()
def eval_command(
    ctx: typer.Context,
    run: Annotated[str | None, typer.Option("--run", help="Finalized run to evaluate.")] = None,
    truth: Annotated[
        Path | None, typer.Option(help="Ground truth JSONL (default: inferred from the run).")
    ] = None,
    out: OutOpt = None,
) -> None:
    """Evaluate one run: ``reports/eval_<run>.md`` with charts, stored for the API."""
    if ctx.invoked_subcommand is not None:
        return
    if run is None:
        raise typer.BadParameter("Pass --run RUN_ID, or use: rfq eval compare --runs A B C.")
    from rfq_agent.eval.report import write_eval_report
    from rfq_agent.eval.run_eval import EvalRefused, evaluate_run
    from rfq_agent.store.runs import open_store

    with open_store(get_settings().db_path) as store:
        try:
            result = evaluate_run(store, run, resolve_path(truth) if truth else None)
        except KeyError as exc:
            raise typer.BadParameter(f"Unknown run {run!r}.") from exc
        except EvalRefused as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc
    paths = write_eval_report(result, _out_dir(out))
    _echo_eval(result)
    typer.echo(f"report: {paths[0]}")


@eval_app.command("compare")
def compare_command(
    runs: Annotated[list[str] | None, typer.Option("--runs", help="Runs to compare.")] = None,
    more: Annotated[list[str] | None, typer.Argument(help="More runs (--runs A B C).")] = None,
    out: OutOpt = None,
) -> None:
    """One comparison table over evaluated runs of the same RFQ (strategies, models)."""
    from rfq_agent.eval.report import render_compare_markdown, write_compare_report
    from rfq_agent.eval.run_eval import EvalRefused, stored_evaluations
    from rfq_agent.store.runs import open_store

    run_ids = [*(runs or []), *(more or [])]
    if len(run_ids) < 2:
        raise typer.BadParameter("Compare at least two runs: rfq eval compare --runs A B.")
    with open_store(get_settings().db_path) as store:
        try:
            evals = stored_evaluations(store, run_ids)
        except EvalRefused as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    paths = write_compare_report(evals, _out_dir(out), stamp=stamp)
    typer.echo(render_compare_markdown(evals))
    typer.echo(f"report: {paths[0]}")


@eval_app.command("inject")
def inject_command(
    run_id: Annotated[str, typer.Argument(help="Finalized run whose stage outcomes to copy.")],
    rows: Annotated[int, typer.Option(min=1, help="Rows to make wrong.")] = 40,
    seed: Annotated[int, typer.Option(help="Seed for drawing the rows.")] = 7,
    trace: TraceOpt = True,
    output_dir: OutputDirOpt = None,
) -> None:
    """Copy RUN_ID with known errors in some rows and finalize it with ``evidence``.

    The spec 14 injection experiment: re-score the copy with other strategies, evaluate,
    and see whether their scores hold the injected rows back. No LLM call.
    """
    from rfq_agent.eval.inject import KINDS, InjectionRefused, inject_errors
    from rfq_agent.store.runs import RescoreRefused, open_store

    settings = get_settings()
    with open_store(settings.db_path) as store:
        try:
            store.check_rescorable(run_id)
            wrong, injections = inject_errors(
                store.rows(run_id), store.outcomes(run_id), rows, seed
            )
        except KeyError as exc:
            raise typer.BadParameter(f"Unknown run {run_id!r}.") from exc
        except (RescoreRefused, InjectionRefused) as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc
        new_id = store.copy_for_injection(run_id, wrong, injections)
        spec = store.run(new_id).spec
    kinds = Counter(injections.values())
    typer.echo(f"injected {len(injections)} rows: " + ", ".join(f"{k} {kinds[k]}" for k in KINDS))
    drive_copy(settings, new_id, spec, output_dir, trace)


def _out_dir(out: Path | None) -> Path:
    return resolve_path(out) if out else PROJECT_ROOT / "reports"


def _echo_eval(result: RunEval) -> None:
    from rfq_agent.eval.report import injection_summary, pct

    cal = result.calibration
    bands = ", ".join(
        f"{band} {b.rows} (usable {b.usable}, correct {b.row_correct})"
        for band, b in cal.bands.items()
    )
    typer.echo(
        f"run {result.run_id} ({result.strategy}, {result.rows} rows): row correct "
        f"{pct(cal.row_correct_rate)}, usable {pct(cal.usable_rate)}, ECE {cal.ece:.3f}, "
        f"AUROC {'-' if cal.auroc is None else f'{cal.auroc:.3f}'}"
    )
    typer.echo(f"bands: {bands}; red rows correct {pct(cal.red_row_correct_rate)}")
    if result.injection is not None:
        typer.echo(injection_summary(result.injection))
