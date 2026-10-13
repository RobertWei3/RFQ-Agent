"""``rfq config`` and ``rfq llm`` commands."""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Annotated

import typer
import yaml
from langchain_core.messages import HumanMessage

from rfq_agent.config import ConfigError, get_settings
from rfq_agent.llm.ping import PING_PROMPT, PingResponse
from rfq_agent.llm.structured import invoke_structured

config_app = typer.Typer(help="Inspect configuration.", no_args_is_help=True)
llm_app = typer.Typer(help="LLM layer utilities.", no_args_is_help=True)


@config_app.command("show")
def config_show() -> None:
    """Print the merged settings and resolved roles. API keys are never printed."""
    settings = get_settings()
    data = settings.model_dump(mode="json", exclude={"cost_model"})
    roles = ["default", *settings.llm.roles]
    resolved = {}
    for role in roles:
        cfg = settings.llm.for_role(role)
        entry = cfg.model_dump(mode="json")
        key_env = cfg.api_key_env
        entry["api_key"] = (
            "n/a" if not key_env else ("set" if os.environ.get(key_env, "").strip() else "missing")
        )
        resolved[role] = entry
    data["resolved_roles"] = resolved
    typer.echo(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


@llm_app.command("ping")
def llm_ping(
    role: Annotated[str, typer.Option(help="Model role to test.")] = "default",
    allow_real: Annotated[
        bool, typer.Option("--allow-real", help="Permit a real (paid) API call.")
    ] = False,
) -> None:
    """Send one structured-output request to verify the LLM path end to end."""
    settings = get_settings()
    cfg = settings.llm.for_role(role)
    if cfg.provider != "fake" and not allow_real:
        typer.echo(
            f"Role {role!r} resolves to real provider {cfg.provider!r} ({cfg.model}). "
            "Re-run with --allow-real, or set RFQ_LLM_PROVIDER=fake.",
            err=True,
        )
        raise typer.Exit(code=2)

    token = uuid.uuid4().hex[:8]
    try:
        result = asyncio.run(
            invoke_structured(
                PingResponse,
                [HumanMessage(content=PING_PROMPT.format(token=token))],
                role=role,
                run_id=f"ping-{token}",
                agent="ping",
                settings=settings,
            )
        )
    except ConfigError as exc:
        typer.echo(f"Config error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if not result.ok or result.value is None:
        typer.echo(f"FAILED after {result.attempts} attempt(s): {result.error}", err=True)
        raise typer.Exit(code=1)
    status = "OK" if result.value.echo == token else "MISMATCH"
    typer.echo(
        f"{status} provider={cfg.provider} model={cfg.model} attempts={result.attempts} "
        f"response={result.value.model_dump()}"
    )
    if status != "OK":
        raise typer.Exit(code=1)
