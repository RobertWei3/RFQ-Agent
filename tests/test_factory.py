"""Factory tests. Real providers are only constructed, never called."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from langchain_anthropic import ChatAnthropic
from langchain_deepseek import ChatDeepSeek
from langchain_openai import ChatOpenAI

from rfq_agent.config import ConfigError, load_settings
from rfq_agent.llm.factory import get_chat_model
from rfq_agent.llm.fake import FakeChatModel
from rfq_agent.llm.tracking import META_ROLE, TrackingCallback


def test_fake_mode_returns_fake_model(settings) -> None:  # type: ignore[no-untyped-def]
    model = get_chat_model("judge", settings=settings)
    assert isinstance(model, FakeChatModel)
    assert (model.metadata or {})[META_ROLE] == "judge"
    assert any(isinstance(cb, TrackingCallback) for cb in model.callbacks or [])


def test_track_false_attaches_no_callback(settings) -> None:  # type: ignore[no-untyped-def]
    assert not get_chat_model(settings=settings, track=False).callbacks


@pytest.mark.usefixtures("real_provider_env")
def test_deepseek_built_with_thinking_disabled(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    settings = load_settings(config_dir)

    model = get_chat_model("default", settings=settings)
    judge = get_chat_model("judge", settings=settings)

    assert isinstance(model, ChatDeepSeek)
    assert model.model_name == "deepseek-flash"
    assert model.extra_body == {"thinking": {"type": "disabled"}}
    assert isinstance(judge, ChatDeepSeek) and judge.model_name == "deepseek-v4-pro"


@pytest.mark.usefixtures("real_provider_env")
def test_missing_api_key_raises_clear_error(config_dir: Path) -> None:
    settings = load_settings(config_dir)
    with pytest.raises(ConfigError, match="DEEPSEEK_API_KEY"):
        get_chat_model("default", settings=settings)


@pytest.mark.usefixtures("real_provider_env")
def test_qwen_and_anthropic_providers(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = config_dir / "settings.yaml"
    data = yaml.safe_load(path.read_text())
    data["llm"]["roles"]["q"] = {"provider": "qwen"}
    data["llm"]["roles"]["a"] = {"provider": "anthropic"}
    path.write_text(yaml.safe_dump(data))
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-q")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-a")
    settings = load_settings(config_dir)

    qwen = get_chat_model("q", settings=settings)
    claude = get_chat_model("a", settings=settings)

    assert isinstance(qwen, ChatOpenAI) and qwen.model_name == "qwen-plus"
    assert qwen.openai_api_base is not None and "dashscope" in qwen.openai_api_base
    assert isinstance(claude, ChatAnthropic) and claude.model == "claude-sonnet-5-5"
