"""``rfq api serve``: the FastAPI backend (spec 15.1)."""

from __future__ import annotations

from typing import Annotated

import typer

from rfq_agent.config import get_settings

api_app = typer.Typer(help="HTTP API.", no_args_is_help=True)


@api_app.command("serve")
def api_serve(
    host: Annotated[str, typer.Option(help="Interface to bind.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to listen on.")] = 8000,
    allow_real: Annotated[
        bool, typer.Option("--allow-real", help="Let API-started work make paid LLM calls.")
    ] = False,
) -> None:
    """Serve the API; runs and reviews that rerun agents go to worker processes."""
    import uvicorn

    from rfq_agent.api import create_app

    uvicorn.run(create_app(get_settings(), allow_real=allow_real), host=host, port=port)
