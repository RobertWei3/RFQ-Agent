"""Local run trace: one JSONL event per agent step and per LLM call.

Events are written to ``<output>.trace.jsonl`` next to the run output. Nothing
leaves the machine. ``rfq trace show`` renders a trace per row.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from pydantic import BaseModel

META_ROW_ID = "row_id"


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [_jsonable(v) for v in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


class TraceWriter:
    """Thread-safe JSONL event sink (LLM callbacks may run in worker threads)."""

    def __init__(self, path: Path, run_id: str, *, append: bool = False) -> None:
        self.path = path
        self.run_id = run_id
        self._lock = threading.Lock()
        self._seq = 0
        path.parent.mkdir(parents=True, exist_ok=True)
        if not append:
            path.write_text("", encoding="utf-8")

    def event(self, row_id: int | None, agent: str, step: str, **data: Any) -> None:
        with self._lock:
            self._seq += 1
            record = {
                "seq": self._seq,
                "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
                "run_id": self.run_id,
                "row_id": row_id,
                "agent": agent,
                "step": step,
                "data": _jsonable(data),
            }
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _message_dict(message: BaseMessage) -> dict[str, Any]:
    data: dict[str, Any] = {"role": message.type, "content": message.content}
    if isinstance(message, AIMessage) and message.tool_calls:
        data["tool_calls"] = [{"name": c["name"], "args": c["args"]} for c in message.tool_calls]
    return data


class TraceLLMCallback(BaseCallbackHandler):
    """Records the full prompt, bound tool names and raw response of every chat call."""

    def __init__(self, writer: TraceWriter) -> None:
        self.writer = writer
        self._pending: dict[
            UUID, tuple[float, dict[str, Any], list[dict[str, Any]], list[str]]
        ] = {}

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        params = kwargs.get("invocation_params") or {}
        tools = [t.get("function", {}).get("name", "?") for t in params.get("tools") or []]
        prompt = [_message_dict(m) for m in (messages[0] if messages else [])]
        self._pending[run_id] = (time.perf_counter(), dict(metadata or {}), prompt, tools)

    def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        pending = self._pending.pop(run_id, None)
        if pending is None:
            return
        started, meta, prompt, tools = pending
        generation = response.generations[0][0] if response.generations else None
        output = (
            _message_dict(generation.message) if isinstance(generation, ChatGeneration) else None
        )
        usage = getattr(getattr(generation, "message", None), "usage_metadata", None)
        self.writer.event(
            meta.get(META_ROW_ID),
            str(meta.get("agent")),
            "llm_call",
            model=meta.get("ls_model_name"),
            tools=tools,
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
            usage=dict(usage) if usage else None,
            prompt=prompt,
            response=output,
        )

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        pending = self._pending.pop(run_id, None)
        if pending is None:
            return
        _, meta, prompt, tools = pending
        self.writer.event(
            meta.get(META_ROW_ID),
            str(meta.get("agent")),
            "llm_error",
            tools=tools,
            prompt=prompt,
            error=f"{type(error).__name__}: {error}",
        )


def load_trace(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def render_row(events: list[dict[str, Any]], row_id: int, *, full: bool = False) -> str:
    """Human-readable step-by-step view of one row."""
    limit = None if full else 600
    out: list[str] = []
    for ev in sorted((e for e in events if e["row_id"] == row_id), key=lambda e: e["seq"]):
        data = ev["data"]
        out.append(f"── [{ev['seq']}] {ev['agent']} · {ev['step']}")
        if ev["step"] == "llm_call":
            usage = data.get("usage") or {}
            out.append(
                f"   model={data.get('model')} tools={data.get('tools')} "
                f"latency={data.get('latency_ms')}ms tokens in/out="
                f"{usage.get('input_tokens')}/{usage.get('output_tokens')}"
            )
            for msg in data.get("prompt", []):
                out.append(f"   ▸ {msg['role']}: {_clip(str(msg['content']), limit)}")
            response = data.get("response") or {}
            if response.get("content"):
                out.append(f"   ◂ content: {_clip(str(response['content']), limit)}")
            for call in response.get("tool_calls", []):
                args = json.dumps(call["args"], ensure_ascii=False)
                out.append(f"   ◂ {call['name']}: {_clip(args, limit)}")
        else:
            for key, value in data.items():
                text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                out.append(f"   {key}: {_clip(text, limit)}")
    return "\n".join(out) if out else f"(no events for row {row_id})"


def _clip(text: str, limit: int | None) -> str:
    text = text.replace("\n", "\n     ")
    if limit is None or len(text) <= limit:
        return text
    return text[:limit] + f" … (+{len(text) - limit} chars, use --full)"
