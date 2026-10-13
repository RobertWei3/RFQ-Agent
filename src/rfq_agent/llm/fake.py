"""FakeLLM: a deterministic, zero-cost chat model for tests and offline runs.

Structured output works through LangChain's default ``with_structured_output``,
which calls ``bind_tools`` and parses the first tool call. The fake answers a
tool call by dispatching to a rule-based handler registered under the tool
(schema) name. Handlers must derive their answer from the prompt text only and
must never read ground truth. Accuracy is not a goal; valid structure is.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel, LangSmithParams, LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool

FAKE_MODEL_NAME = "fake"


@dataclass(frozen=True)
class FakeRequest:
    """What a handler sees: the prompt and the requested output schema."""

    messages: Sequence[BaseMessage]
    tool_name: str
    json_schema: dict[str, Any]

    @property
    def text(self) -> str:
        """All message contents joined, for regex-style extraction."""
        return "\n".join(_content_text(m) for m in self.messages)


FakeHandler = Callable[[FakeRequest], dict[str, Any]]

_HANDLERS: dict[str, FakeHandler] = {}


class FakeHandlerMissingError(LookupError):
    """Raised when no handler is registered for the requested tool name."""


def register_fake_handler(tool_name: str) -> Callable[[FakeHandler], FakeHandler]:
    """Decorator registering ``fn`` as the fake answer for ``tool_name``.

    ``tool_name`` is the schema class name (e.g. ``"OEResolution"``). Registering
    the same name again replaces the previous handler.
    """

    def decorator(fn: FakeHandler) -> FakeHandler:
        _HANDLERS[tool_name] = fn
        return fn

    return decorator


def unregister_fake_handler(tool_name: str) -> None:
    _HANDLERS.pop(tool_name, None)


def registered_fake_handlers() -> list[str]:
    return sorted(_HANDLERS)


class FakeChatModel(BaseChatModel):
    """Deterministic chat model that supports tool calling via registered handlers."""

    model: str = FAKE_MODEL_NAME

    @property
    def _llm_type(self) -> str:
        return "rfq-fake"

    def _get_ls_params(self, stop: list[str] | None = None, **kwargs: Any) -> LangSmithParams:
        params = super()._get_ls_params(stop=stop, **kwargs)
        params["ls_provider"] = "fake"
        params["ls_model_name"] = self.model
        return params

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        formatted = [convert_to_openai_tool(tool) for tool in tools]
        return self.bind(tools=formatted, tool_choice=tool_choice, **kwargs)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        tools: list[dict[str, Any]] = kwargs.get("tools") or []
        prompt_text = "\n".join(_content_text(m) for m in messages)

        if tools:
            function = _choose_tool(tools, kwargs.get("tool_choice"))["function"]
            name: str = function["name"]
            handler = _HANDLERS.get(name)
            if handler is None:
                raise FakeHandlerMissingError(f"No fake handler registered for {name!r}.")
            args = handler(FakeRequest(messages, name, function.get("parameters", {})))
            message = AIMessage(
                content="",
                tool_calls=[{"name": name, "args": args, "id": "fake_call_0", "type": "tool_call"}],
            )
            output_text = repr(args)
        else:
            output_text = f"[fake] {prompt_text[-200:]}"
            message = AIMessage(content=output_text)

        input_tokens = approx_tokens(prompt_text)
        output_tokens = approx_tokens(output_text)
        message.usage_metadata = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }
        return ChatResult(generations=[ChatGeneration(message=message)])


def approx_tokens(text: str) -> int:
    """Rough token estimate (4 chars per token) so cost tracking has numbers."""
    return max(1, len(text) // 4)


def _choose_tool(tools: list[dict[str, Any]], tool_choice: Any) -> dict[str, Any]:
    if isinstance(tool_choice, str) and tool_choice not in {"any", "auto", "required"}:
        for tool in tools:
            if tool["function"]["name"] == tool_choice:
                return tool
    return tools[0]


def _content_text(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    parts = [p if isinstance(p, str) else str(p.get("text", "")) for p in content]
    return "".join(parts)
