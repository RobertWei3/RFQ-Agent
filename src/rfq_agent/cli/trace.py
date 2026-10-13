"""``rfq trace`` commands."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Annotated

import typer

from rfq_agent.config import resolve_path

trace_app = typer.Typer(help="Inspect local run traces.", no_args_is_help=True)


@trace_app.command("show")
def trace_show(
    path: Annotated[Path, typer.Argument(help="A *.trace.jsonl file.")],
    row: Annotated[int | None, typer.Option(help="Row to show (omit to list rows).")] = None,
    full: Annotated[bool, typer.Option(help="Do not truncate prompts and outputs.")] = False,
) -> None:
    """Show the step-by-step reasoning trace of one row, or list all rows."""
    from rfq_agent.trace import load_trace, render_row

    events = load_trace(resolve_path(path))
    if row is not None:
        typer.echo(render_row(events, row, full=full))
        return
    by_row: dict[int, list[dict[str, object]]] = defaultdict(list)
    for event in events:
        if event["row_id"] is not None:
            by_row[event["row_id"]].append(event)
    typer.echo(f"{'row':>4}  {'llm calls':>9}  outcome")
    for row_id in sorted(by_row):
        row_events = by_row[row_id]
        calls = sum(e["step"] == "llm_call" for e in row_events)
        typer.echo(f"{row_id:>4}  {calls:>9}  {_outcome(row_events[-1])}")


def _outcome(last: dict[str, object]) -> str:
    data = last["data"]
    assert isinstance(data, dict)
    if last["step"] == "build_resolution":
        return f"{data['status']} {data.get('current_oe') or ''}".strip()
    return f"{last['step']} {data.get('stage', '')} {data.get('error', '')}".strip()
