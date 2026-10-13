"""Chat model factory: ``get_chat_model(role)`` returns the configured LangChain model."""

from __future__ import annotations

import os

from langchain_anthropic import ChatAnthropic
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_deepseek import ChatDeepSeek
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from rfq_agent.config import ConfigError, ModelConfig, Settings, get_settings
from rfq_agent.llm.fake import FakeChatModel
from rfq_agent.llm.tracking import META_ROLE, TrackingCallback


def get_chat_model(
    role: str = "default",
    *,
    settings: Settings | None = None,
    track: bool = True,
) -> BaseChatModel:
    """Build the chat model for ``role``, with usage tracking attached by default."""
    settings = settings or get_settings()
    cfg = settings.llm.for_role(role)
    callbacks: list[BaseCallbackHandler] = []
    if track:
        callbacks.append(TrackingCallback(settings.db_path, settings.llm.pricing_per_million))
    model = _build(cfg)
    model.callbacks = callbacks
    model.metadata = {**(model.metadata or {}), META_ROLE: role}
    return model


def _build(cfg: ModelConfig) -> BaseChatModel:
    if cfg.provider == "fake":
        return FakeChatModel(model=cfg.model)
    api_key = _api_key(cfg)
    if cfg.provider == "deepseek":
        kwargs = {"api_base": cfg.base_url} if cfg.base_url else {}
        thinking = "enabled" if cfg.thinking else "disabled"
        return ChatDeepSeek(
            model=cfg.model,
            api_key=api_key,
            temperature=cfg.temperature,
            timeout=cfg.timeout_s,
            max_retries=cfg.http_max_retries,
            extra_body={"thinking": {"type": thinking}},
            **kwargs,
        )
    if cfg.provider == "qwen":
        return ChatOpenAI(
            model=cfg.model,
            api_key=api_key,
            base_url=cfg.base_url,
            temperature=cfg.temperature,
            timeout=cfg.timeout_s,
            max_retries=cfg.http_max_retries,
        )
    if cfg.provider == "anthropic":
        kwargs = {"base_url": cfg.base_url} if cfg.base_url else {}
        return ChatAnthropic(
            model=cfg.model,
            api_key=api_key,
            temperature=cfg.temperature,
            timeout=cfg.timeout_s,
            max_retries=cfg.http_max_retries,
            **kwargs,
        )
    raise ConfigError(f"Unsupported provider: {cfg.provider!r}")


def _api_key(cfg: ModelConfig) -> SecretStr:
    if not cfg.api_key_env:
        raise ConfigError(f"Provider {cfg.provider!r} has no api_key_env configured.")
    value = os.environ.get(cfg.api_key_env, "").strip()
    if not value:
        raise ConfigError(
            f"Role {cfg.role!r} needs ${cfg.api_key_env} (set it in .env), "
            f"or set RFQ_LLM_PROVIDER=fake."
        )
    return SecretStr(value)
