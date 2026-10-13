"""Typer CLI entry point (``rfq``). Each command group lives in its own module."""

from __future__ import annotations

import typer

from rfq_agent.cli import agent, api, data, evaluate, run, settings, trace, worksheet

app = typer.Typer(help="RFQ-Agent: automated RFQ pricing pipeline.", no_args_is_help=True)
app.add_typer(settings.config_app, name="config")
app.add_typer(settings.llm_app, name="llm")
app.add_typer(data.data_app, name="data")
app.add_typer(data.tools_app, name="tools")
app.add_typer(data.mcp_app, name="mcp")
app.add_typer(agent.agent_app, name="agent")
app.add_typer(trace.trace_app, name="trace")
app.add_typer(worksheet.worksheet_app, name="worksheet")
app.command("run")(run.run_command)
app.command("rerun-failed")(run.rerun_failed_command)
app.command("rescore")(run.rescore_command)
app.add_typer(run.runs_app, name="runs")
app.add_typer(api.api_app, name="api")
app.add_typer(evaluate.eval_app, name="eval")

__all__ = ["app"]
