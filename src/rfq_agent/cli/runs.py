"""Helpers shared by the agent commands: run files, cost guard, ground truth lookup."""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from rfq_agent.config import PROJECT_ROOT, resolve_path

if TYPE_CHECKING:
    from rfq_agent.config import Settings
    from rfq_agent.data_gen.truth import GroundTruthRow
    from rfq_agent.schemas import RFQLine
    from rfq_agent.trace import TraceWriter


def read_lines(input: Path, limit: int | None) -> list[RFQLine]:
    from rfq_agent.intake import read_rfq

    intake = read_rfq(resolve_path(input))
    if intake.skipped:
        typer.echo(f"intake skipped {len(intake.skipped)} row(s): {intake.skipped}")
    return intake.lines[:limit] if limit else intake.lines


def announce_and_guard(
    settings: Settings, role: str, rows: int, calls: tuple[int, int], allow_real: bool
) -> None:
    """Print the expected LLM call count, then refuse real providers without --allow-real."""
    cfg = settings.llm.for_role(role)
    typer.echo(f"rows={rows} provider={cfg.provider} model={cfg.model}")
    typer.echo(f"LLM calls: expected ~{calls[0]}, upper bound {calls[1]}")
    guard_real(settings, [role], allow_real)


def guard_real(settings: Settings, roles: Iterable[str], allow_real: bool) -> None:
    """Refuse (exit 2) when any of ``roles`` uses a real provider and --allow-real is off."""
    providers = {role: settings.llm.for_role(role).provider for role in roles}
    paid = [f"{role!r} ({provider})" for role, provider in providers.items() if provider != "fake"]
    if paid and not allow_real:
        typer.echo(
            f"Real LLM provider for role {', '.join(paid)}; re-run with --allow-real to proceed.",
            err=True,
        )
        raise typer.Exit(code=2)


def count_failures(outcomes: Sequence[object]) -> int:
    from rfq_agent.agents.common import RowFailure

    return sum(isinstance(o, RowFailure) for o in outcomes)


@dataclass
class RunFiles:
    """Output, trace, meta and comparison files of one agent run (same stem)."""

    agent: str
    run_id: str
    output: Path
    writer: TraceWriter | None

    @classmethod
    def create(cls, agent: str, output: Path | None, trace: bool) -> RunFiles:
        from datetime import UTC, datetime

        from rfq_agent.trace import TraceWriter

        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        run_id = f"{agent}-{stamp}-{uuid.uuid4().hex[:6]}"
        path = (
            resolve_path(output)
            if output
            else PROJECT_ROOT / "data" / "runs" / (f"{agent}_{stamp}.jsonl")
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        writer = TraceWriter(path.with_suffix(".trace.jsonl"), run_id) if trace else None
        return cls(agent, run_id, path, writer)

    def finish(self, settings: Settings, rows: int, failed: int, **info: object) -> None:
        from rfq_agent.agents.runner import write_meta

        cfg = settings.llm.for_role(self.agent)
        write_meta(
            self.output,
            agent=self.agent,
            run_id=self.run_id,
            provider=cfg.provider,
            model=cfg.model,
            rows=rows,
            failed=failed,
            **info,
        )
        typer.echo(f"run_id={self.run_id} wrote {rows} rows ({failed} failed) to {self.output}")
        print_usage(settings.db_path, self.run_id)
        if self.writer is not None:
            trace_path = self.writer.path
            typer.echo(f"trace: {trace_path}  (view with: rfq trace show {trace_path} --row 1)")

    def write_report(self, report: str) -> None:
        report_path = self.output.with_suffix(".compare.md")
        report_path.write_text(report + "\n", encoding="utf-8")
        typer.echo(report.splitlines()[-1])
        typer.echo(f"comparison table: {report_path}")


def default_truth(input_path: Path, dataset_dir: Path) -> Path | None:
    """Ground truth that belongs to a sample workbook or a dataset's own workbook."""
    name = input_path.name
    if name.startswith("rfq_sample_"):
        return dataset_dir / f"{input_path.stem}_ground_truth.jsonl"
    if resolve_path(input_path).parent == dataset_dir:
        return dataset_dir / "ground_truth.jsonl"
    return None


def load_truth_file(path: Path) -> list[GroundTruthRow]:
    from rfq_agent.data_gen.truth import GroundTruthRow

    with path.open(encoding="utf-8") as fh:
        return [GroundTruthRow.model_validate_json(line) for line in fh]


def print_usage(db_path: Path, run_id: str) -> None:
    from rfq_agent.store.db import connect

    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*), SUM(input_tokens), SUM(output_tokens), SUM(cost_usd), "
            "SUM(status = 'error') FROM llm_calls WHERE rfq_run_id = ?",
            (run_id,),
        ).fetchone()
    conn.close()
    calls, tokens_in, tokens_out, cost, errors = row
    typer.echo(
        f"LLM usage: calls={calls} errors={errors or 0} input_tokens={tokens_in or 0} "
        f"output_tokens={tokens_out or 0} cost_usd={cost if cost is not None else 'n/a'}"
    )
