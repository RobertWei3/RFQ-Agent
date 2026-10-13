from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from langchain_core.messages import HumanMessage
from pydantic import BaseModel

from rfq_agent.config import Settings
from rfq_agent.llm import structured as structured_mod
from rfq_agent.llm.fake import FakeRequest, register_fake_handler, unregister_fake_handler
from rfq_agent.llm.structured import invoke_structured


class Answer(BaseModel):
    value: int


@pytest.fixture
def answer_handler() -> Iterator[list[FakeRequest]]:
    """Returns invalid output for the first N calls (configurable), then a valid one."""
    seen: list[FakeRequest] = []

    @register_fake_handler("Answer")
    def handler(request: FakeRequest) -> dict[str, Any]:
        seen.append(request)
        bad_calls = int(request.messages[0].content.split("bad=")[1])  # type: ignore[union-attr]
        return {"value": "not-an-int"} if len(seen) <= bad_calls else {"value": 42}

    yield seen
    unregister_fake_handler("Answer")


async def test_success_first_try(settings: Settings, answer_handler: list[FakeRequest]) -> None:
    result = await invoke_structured(
        Answer, [HumanMessage(content="bad=0")], role="default", settings=settings
    )
    assert result.ok and result.value == Answer(value=42) and result.attempts == 1


async def test_retry_feeds_error_back(
    settings: Settings, answer_handler: list[FakeRequest]
) -> None:
    result = await invoke_structured(
        Answer, [HumanMessage(content="bad=2")], role="default", settings=settings
    )
    assert result.ok and result.attempts == 3
    retry_prompt = answer_handler[1].messages[-1].content
    assert "could not be validated as Answer" in retry_prompt  # type: ignore[operator]
    assert "value" in retry_prompt  # type: ignore[operator]


async def test_exhausted_retries_return_failure(
    settings: Settings, answer_handler: list[FakeRequest]
) -> None:
    result = await invoke_structured(
        Answer, [HumanMessage(content="bad=10")], role="default", settings=settings
    )
    assert not result.ok and result.value is None
    assert result.attempts == 3  # 1 call + 2 retries from settings
    assert result.error


async def test_model_exception_does_not_raise(settings: Settings) -> None:
    result = await invoke_structured(
        Answer, [HumanMessage(content="x")], role="default", settings=settings
    )  # no handler registered -> FakeHandlerMissingError inside the call
    assert not result.ok and result.error is not None
    assert "FakeHandlerMissingError" in result.error


async def test_semaphore_caps_concurrency(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.llm.max_concurrency = 2
    active = peak = 0

    @register_fake_handler("Answer")
    def handler(request: FakeRequest) -> dict[str, Any]:
        return {"value": 1}

    original = structured_mod.llm_semaphore

    class Probe:
        def __init__(self, sem: asyncio.Semaphore) -> None:
            self.sem = sem

        async def __aenter__(self) -> None:
            nonlocal active, peak
            await self.sem.acquire()
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)

        async def __aexit__(self, *exc: object) -> None:
            nonlocal active
            active -= 1
            self.sem.release()

    monkeypatch.setattr(structured_mod, "llm_semaphore", lambda n: Probe(original(n)))
    try:
        results = await asyncio.gather(
            *(
                invoke_structured(Answer, [HumanMessage(content="q")], role="r", settings=settings)
                for _ in range(6)
            )
        )
    finally:
        unregister_fake_handler("Answer")
    assert all(r.ok for r in results)
    assert peak == 2
