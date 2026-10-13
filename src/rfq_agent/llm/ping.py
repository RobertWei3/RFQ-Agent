"""Smoke-test schema used by ``rfq llm ping``, with its FakeLLM handler."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from rfq_agent.llm.fake import FakeRequest, register_fake_handler

PING_PROMPT = "Reply with ok=true and echo the token {token}."


class PingResponse(BaseModel):
    """Minimal structured answer proving the structured-output path works."""

    ok: bool
    echo: str = Field(description="The token from the prompt, verbatim.")


@register_fake_handler(PingResponse.__name__)
def _fake_ping(request: FakeRequest) -> dict[str, Any]:
    match = re.search(r"echo the token (\S+?)\.", request.text)
    return {"ok": True, "echo": match.group(1) if match else ""}
