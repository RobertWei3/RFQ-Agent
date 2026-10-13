"""Structured LLM invocation with validation retries and a global concurrency cap.

On a validation failure the error is appended to the prompt and the call is
retried (``pipeline.structured_output_retries`` times by default). A call that
still fails returns ``StructuredResult(ok=False)`` instead of raising, so one
bad row can never crash a batch.
"""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import Mapping, Sequence
from typing import Any, Generic, TypeVar

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel

from rfq_agent.config import Settings, get_settings
from rfq_agent.llm.factory import get_chat_model
from rfq_agent.llm.tracking import META_AGENT, META_RUN_ID

T = TypeVar("T", bound=BaseModel)

_SEMAPHORES: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


class StructuredResult(BaseModel, Generic[T]):
    """Outcome of ``invoke_structured``: a parsed value or the final error."""

    ok: bool
    value: T | None = None
    error: str | None = None
    attempts: int


def llm_semaphore(max_concurrency: int) -> asyncio.Semaphore:
    """The process-wide LLM semaphore for the running event loop."""
    loop = asyncio.get_running_loop()
    semaphore = _SEMAPHORES.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(max_concurrency)
        _SEMAPHORES[loop] = semaphore
    return semaphore


async def invoke_structured(
    schema: type[T],
    messages: Sequence[BaseMessage],
    *,
    role: str,
    run_id: str | None = None,
    agent: str | None = None,
    model: BaseChatModel | None = None,
    retries: int | None = None,
    settings: Settings | None = None,
    metadata: Mapping[str, Any] | None = None,
    callbacks: Sequence[BaseCallbackHandler] | None = None,
) -> StructuredResult[T]:
    """Call the model for ``role`` and parse its output into ``schema``."""
    settings = settings or get_settings()
    model = model or get_chat_model(role, settings=settings)
    max_retries = settings.pipeline.structured_output_retries if retries is None else retries
    runnable = model.with_structured_output(schema, include_raw=True)
    config: RunnableConfig = {
        "metadata": {**(metadata or {}), META_RUN_ID: run_id, META_AGENT: agent or role},
        "run_name": agent or role,
    }
    if callbacks:
        config["callbacks"] = list(callbacks)

    prompt = list(messages)
    error = "not attempted"
    attempts = 0
    for _ in range(max_retries + 1):
        attempts += 1
        try:
            async with llm_semaphore(settings.llm.max_concurrency):
                output: dict[str, Any] = await runnable.ainvoke(prompt, config=config)  # type: ignore[assignment]
        except Exception as exc:  # noqa: BLE001 - row-level failure must not crash the batch
            return StructuredResult(
                ok=False, error=f"{type(exc).__name__}: {exc}", attempts=attempts
            )

        parsed = output.get("parsed")
        if isinstance(parsed, schema):
            return StructuredResult(ok=True, value=parsed, attempts=attempts)

        parsing_error = output.get("parsing_error")
        error = str(parsing_error) if parsing_error else "Model returned no structured output."
        prompt = [*prompt, HumanMessage(content=_retry_instruction(schema, error))]

    return StructuredResult(ok=False, error=error, attempts=attempts)


def _retry_instruction(schema: type[BaseModel], error: str) -> str:
    return (
        f"Your previous output could not be validated as {schema.__name__}.\n"
        f"Validation error:\n{error}\n"
        f"Call the {schema.__name__} tool again with corrected arguments."
    )
