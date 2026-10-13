from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from langchain_core.messages import HumanMessage
from pydantic import BaseModel

from rfq_agent.llm.fake import (
    FakeChatModel,
    FakeHandlerMissingError,
    FakeRequest,
    register_fake_handler,
    unregister_fake_handler,
)
from rfq_agent.schemas import Evidence, OECandidate


class Unregistered(BaseModel):
    value: int


@pytest.fixture
def oe_candidate_handler() -> Iterator[None]:
    @register_fake_handler("OECandidate")
    def handler(request: FakeRequest) -> dict[str, Any]:
        oe = request.text.split("OE:")[1].split()[0]
        return {
            "oe_part_no": oe,
            "brand": None,
            "match_type": "exact",
            "evidence": [{"source": "oem_catalog", "ref": "page-7", "snippet": request.text[:50]}],
        }

    yield
    unregister_fake_handler("OECandidate")


@pytest.mark.usefixtures("oe_candidate_handler")
def test_structured_output_returns_section7_model() -> None:
    structured = FakeChatModel().with_structured_output(OECandidate)
    result = structured.invoke([HumanMessage(content="Catalog page. OE: 1234-ABC fits model X")])

    assert isinstance(result, OECandidate)
    assert result.oe_part_no == "1234-ABC"
    assert result.evidence == [
        Evidence(
            source="oem_catalog", ref="page-7", snippet="Catalog page. OE: 1234-ABC fits model X"
        )
    ]


@pytest.mark.usefixtures("oe_candidate_handler")
async def test_async_structured_output_and_usage() -> None:
    raw = (
        await FakeChatModel()
        .with_structured_output(OECandidate, include_raw=True)
        .ainvoke("OE: 99-X")
    )
    assert raw["parsed"].oe_part_no == "99-X"
    usage = raw["raw"].usage_metadata
    assert usage["input_tokens"] > 0 and usage["output_tokens"] > 0


@pytest.mark.usefixtures("oe_candidate_handler")
def test_fake_is_deterministic() -> None:
    model = FakeChatModel().with_structured_output(OECandidate)
    assert model.invoke("OE: A-1") == model.invoke("OE: A-1")


def test_missing_handler_raises() -> None:
    with pytest.raises(FakeHandlerMissingError, match="Unregistered"):
        FakeChatModel().with_structured_output(Unregistered).invoke("hi")


def test_plain_text_invocation() -> None:
    message = FakeChatModel().invoke("hello")
    assert message.content == "[fake] hello"
