"""Per-call LLM usage tracking: tokens, cost and latency written to ``llm_calls``.

Pass ``rfq_run_id`` and ``agent`` through ``RunnableConfig.metadata``. The chat
model's own ``metadata`` (set by the factory) supplies ``llm_role``.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import weakref
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from rfq_agent.config import Pricing
from rfq_agent.store.db import connect

META_RUN_ID = "rfq_run_id"
META_AGENT = "agent"
META_ROLE = "llm_role"


@dataclass
class _PendingCall:
    started: float
    metadata: dict[str, Any]


def compute_cost(input_tokens: int, output_tokens: int, pricing: Pricing | None) -> float | None:
    """USD cost for one call, or ``None`` when the model has no configured price."""
    if pricing is None:
        return None
    return (input_tokens * pricing.input + output_tokens * pricing.output) / 1_000_000


class TrackingCallback(BaseCallbackHandler):
    """Records every chat-model call as one row in the ``llm_calls`` table."""

    def __init__(self, db_path: Path, pricing: dict[str, Pricing]) -> None:
        self.db_path = db_path
        self.pricing = pricing
        self._pending: dict[UUID, _PendingCall] = {}
        # One connection for every call, shared by the executor threads callbacks run in:
        # fewer opens and closes, which SQLite 3.51.0 / 3.51.1 could deadlock on (see
        # store.db.OPEN_CLOSE_LOCK).
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self._pending[run_id] = _PendingCall(time.perf_counter(), dict(metadata or {}))

    def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        pending = self._pending.pop(run_id, None)
        if pending is None:
            return
        input_tokens = output_tokens = cached = 0
        generation = response.generations[0][0] if response.generations else None
        if isinstance(generation, ChatGeneration):
            usage = getattr(generation.message, "usage_metadata", None) or {}
            input_tokens = int(usage.get("input_tokens", 0))
            output_tokens = int(usage.get("output_tokens", 0))
            details = usage.get("input_token_details") or {}
            cached = int(details.get("cache_read", 0) or 0)
        model = pending.metadata.get("ls_model_name")
        if pending.metadata.get("ls_provider") == "fake":
            cost: float | None = 0.0
        else:
            cost = compute_cost(input_tokens, output_tokens, self.pricing.get(str(model)))
        self._write(pending, "ok", None, input_tokens, output_tokens, cached, cost)

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        pending = self._pending.pop(run_id, None)
        if pending is None:
            return
        self._write(pending, "error", f"{type(error).__name__}: {error}", None, None, None, None)

    def _write(
        self,
        pending: _PendingCall,
        status: str,
        error: str | None,
        input_tokens: int | None,
        output_tokens: int | None,
        cached: int | None,
        cost: float | None,
    ) -> None:
        meta = pending.metadata
        latency_ms = (time.perf_counter() - pending.started) * 1000
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                INSERT INTO llm_calls (
                    ts, rfq_run_id, agent, role, provider, model, input_tokens,
                    output_tokens, cached_input_tokens, cost_usd, latency_ms, status, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(UTC).isoformat(),
                    meta.get(META_RUN_ID),
                    meta.get(META_AGENT),
                    meta.get(META_ROLE),
                    meta.get("ls_provider"),
                    meta.get("ls_model_name"),
                    input_tokens,
                    output_tokens,
                    cached,
                    cost,
                    latency_ms,
                    status,
                    error,
                ),
            )

    def _connection(self) -> sqlite3.Connection:
        """The shared connection, opened on first use; closed when the callback goes away."""
        if self._conn is None:
            self._conn = connect(self.db_path, shared=True)
            weakref.finalize(self, self._conn.close)
        return self._conn
