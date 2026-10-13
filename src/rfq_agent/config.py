"""Configuration loading: YAML files under ``config/`` plus secrets from ``.env``."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal, TypeVar, get_args

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, model_validator

from rfq_agent.schemas import MatchType, SpecSource, StrategyName

ProviderName = Literal["deepseek", "anthropic", "qwen", "fake"]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_DIR = PROJECT_ROOT / "config"

ENV_CONFIG_DIR = "RFQ_CONFIG_DIR"
ENV_FORCE_PROVIDER = "RFQ_LLM_PROVIDER"
ENV_SKIP_DOTENV = "RFQ_SKIP_DOTENV"  # set to "1" (e.g. in tests) to ignore the project .env

_T = TypeVar("_T")


class ConfigError(RuntimeError):
    """Raised when configuration is missing or inconsistent."""


class Pricing(BaseModel):
    """Token prices in USD per million tokens."""

    input: float = Field(ge=0)
    output: float = Field(ge=0)


class ProviderConfig(BaseModel):
    """Connection defaults for one provider."""

    base_url: str | None = None
    api_key_env: str | None = None
    default_model: str | None = None


class RoleOverride(BaseModel):
    """Per-role overrides; unset fields inherit from the ``llm`` defaults."""

    provider: ProviderName | None = None
    model: str | None = None
    base_url: str | None = None
    temperature: float | None = None
    thinking: bool | None = None
    timeout_s: float | None = None
    http_max_retries: int | None = None


class ModelConfig(BaseModel):
    """Fully resolved model settings for a single role."""

    role: str
    provider: ProviderName
    model: str
    base_url: str | None
    api_key_env: str | None
    temperature: float
    thinking: bool
    timeout_s: float
    http_max_retries: int
    pricing: Pricing | None


class LLMSettings(BaseModel):
    provider: ProviderName = "deepseek"
    base_url: str | None = None
    model: str
    temperature: float = 0.0
    thinking: bool = False
    timeout_s: float = Field(default=60.0, gt=0)
    http_max_retries: int = Field(default=2, ge=0)
    max_concurrency: int = Field(default=8, ge=1)
    pricing_per_million: dict[str, Pricing] = Field(default_factory=dict)
    roles: dict[str, RoleOverride] = Field(default_factory=dict)
    providers: dict[ProviderName, ProviderConfig] = Field(default_factory=dict)
    force_provider: ProviderName | None = Field(
        default=None, description="Set from RFQ_LLM_PROVIDER; overrides every role."
    )

    def for_role(self, role: str = "default") -> ModelConfig:
        """Resolve the effective model config for ``role``."""
        override = self.roles.get(role, RoleOverride())
        provider: ProviderName = override.provider or self.provider
        provider_cfg = self.providers.get(provider, ProviderConfig())

        if override.model is not None:
            model = override.model
        elif provider == self.provider:
            model = self.model
        elif provider_cfg.default_model is not None:
            model = provider_cfg.default_model
        else:
            raise ConfigError(f"Role {role!r} uses provider {provider!r} but sets no model.")

        base_url: str | None
        if override.base_url is not None:
            base_url = override.base_url
        elif provider == self.provider and self.base_url is not None:
            base_url = self.base_url
        else:
            base_url = provider_cfg.base_url

        api_key_env = provider_cfg.api_key_env

        if self.force_provider is not None and self.force_provider != provider:
            provider = self.force_provider
            forced_cfg = self.providers.get(provider, ProviderConfig())
            model = forced_cfg.default_model or provider
            base_url = forced_cfg.base_url
            api_key_env = forced_cfg.api_key_env

        return ModelConfig(
            role=role,
            provider=provider,
            model=model,
            base_url=base_url,
            api_key_env=api_key_env,
            temperature=_pick(override.temperature, self.temperature),
            thinking=_pick(override.thinking, self.thinking),
            timeout_s=_pick(override.timeout_s, self.timeout_s),
            http_max_retries=_pick(override.http_max_retries, self.http_max_retries),
            pricing=self.pricing_per_million.get(model),
        )


class PipelineSettings(BaseModel):
    batch_size: int = Field(default=100, ge=1)
    structured_output_retries: int = Field(default=2, ge=0)
    retail_outlier_threshold: float = Field(default=0.40, gt=0)
    spec_weight_tolerance: float = Field(default=0.05, ge=0)


class BandSettings(BaseModel):
    green_min: int = Field(default=80, ge=0, le=100)
    yellow_min: int = Field(default=50, ge=0, le=100)

    @model_validator(mode="after")
    def _check_order(self) -> BandSettings:
        # Distinct thresholds keep the red and yellow caps apart; yellow_min >= 1 keeps the
        # red cap (yellow_min - 1) a valid score.
        if not 1 <= self.yellow_min < self.green_min:
            raise ValueError("bands need 1 <= yellow_min < green_min")
        return self


SignalName = Literal[
    "oe_match", "chain", "price_sources", "retail_dispersion", "msrp_retail", "spec_source"
]


def _default_weights() -> dict[SignalName, float]:
    return {
        "oe_match": 0.25,
        "chain": 0.10,
        "price_sources": 0.20,
        "retail_dispersion": 0.15,
        "msrp_retail": 0.10,
        "spec_source": 0.20,
    }


class MsrpRetailLevels(BaseModel):
    """Signal values for the selected-retail / MSRP ratio r (spec 12.2)."""

    at_most_msrp: float = Field(default=1.0, ge=0, le=1)  # r <= 1.0
    up_to_suspicious: float = Field(default=0.5, ge=0, le=1)  # 1.0 < r <= suspicious ratio
    above: float = Field(default=0.0, ge=0, le=1)  # r > suspicious ratio
    missing: float = Field(default=0.5, ge=0, le=1)  # either price missing


class EvidenceSettings(BaseModel):
    """Weights and level values of the deterministic evidence signals (spec 12.2)."""

    weights: dict[SignalName, float] = Field(default_factory=_default_weights)
    oe_match: dict[str, float] = Field(
        default_factory=lambda: {
            "exact": 1.0,
            "normalized": 0.9,
            "interchange": 0.7,
            "llm_inferred": 0.2,
            "manual": 1.0,
        }
    )
    spec_source: dict[str, float] = Field(
        default_factory=lambda: {
            "both_agree": 1.0,
            "internal": 0.8,
            "external": 0.5,
            "conflict": 0.3,
            "missing": 0.0,
        }
    )
    chain_truncated: float = Field(default=0.3, ge=0, le=1)
    msrp_retail_suspicious_ratio: float = Field(default=1.2, gt=1)
    price_sources_full: int = Field(default=4, ge=1)
    msrp_retail: MsrpRetailLevels = Field(default_factory=MsrpRetailLevels)
    single_quote_dispersion: float = Field(default=0.5, ge=0, le=1)

    @model_validator(mode="after")
    def _check_weights(self) -> EvidenceSettings:
        if sum(self.weights.values()) <= 0:
            raise ValueError("evidence weights must sum to a positive number")
        return self

    @model_validator(mode="after")
    def _check_level_keys(self) -> EvidenceSettings:
        # A misspelt key would silently score 0, so the keys must match the contracts.
        for field, values in (("oe_match", MatchType), ("spec_source", SpecSource)):
            have, want = set(getattr(self, field)), set(get_args(values))
            if have != want:
                raise ValueError(f"confidence.evidence.{field} must have keys {sorted(want)}")
        return self


class ConfidenceSettings(BaseModel):
    strategy: StrategyName = "evidence_plus_judge"
    bands: BandSettings = Field(default_factory=BandSettings)
    self_samples: int = Field(default=2, ge=1)
    self_weight: float = Field(default=0.3, ge=0, le=1)
    judge_band: tuple[int, int] = (50, 100)
    judge_weight: float = Field(default=0.5, ge=0, le=1)
    evidence: EvidenceSettings = Field(default_factory=EvidenceSettings)


class StorageSettings(BaseModel):
    db_path: Path = Path("data/rfq.db")
    checkpoint_path: Path = Path("data/checkpoints.db")  # LangGraph checkpoints (spec 13)
    upload_dir: Path = Path("data/uploads")  # RFQ workbooks uploaded through the API
    lease_ttl_s: float = Field(default=60.0, gt=0)  # run lease expires without a heartbeat
    lease_heartbeat_s: float = Field(default=10.0, gt=0)

    @model_validator(mode="after")
    def _lease_outlives_heartbeats(self) -> StorageSettings:
        # A failed refresh is retried only while one more interval plus a lock wait (at
        # most an interval) fit in the TTL; that needs more than two intervals.
        if self.lease_ttl_s <= 2 * self.lease_heartbeat_s:
            raise ValueError("storage.lease_ttl_s must exceed 2 x storage.lease_heartbeat_s")
        return self


class DataSettings(BaseModel):
    dataset_dir: Path = Path("data/generated/n200_s42")


class FaultSettings(BaseModel):
    """Deterministic fault injection for mock tools (off by default)."""

    enabled: bool = False
    timeout_rate: float = Field(default=0.0, ge=0, le=1)
    empty_rate: float = Field(default=0.0, ge=0, le=1)
    seed: int = 0

    @model_validator(mode="after")
    def _check_total(self) -> FaultSettings:
        if self.timeout_rate + self.empty_rate > 1:
            raise ValueError("timeout_rate + empty_rate must not exceed 1")
        return self


class ToolSettings(BaseModel):
    top_k: int = Field(default=5, ge=1)
    max_decoys: int = Field(default=2, ge=0)
    faults: FaultSettings = Field(default_factory=FaultSettings)


class CategoryCost(BaseModel):
    cycle_time_min: float = Field(ge=0)
    packaging: float = Field(ge=0)
    freight_per_unit: float = Field(ge=0)
    inland_freight_per_unit: float = Field(ge=0)


class CostModel(BaseModel):
    """Illustrative should-cost and landed-cost rates (see config/cost_model.yaml)."""

    scrap_rate: float = Field(ge=0)
    labor_rate_per_hr: float = Field(ge=0)
    machine_rate_per_hr: float = Field(ge=0)
    overhead_rate: float = Field(ge=0)
    sga_rate: float = Field(ge=0)
    profit_rate: float = Field(ge=0)
    insurance_rate: float = Field(ge=0)
    mfn_duty_rate: float = Field(ge=0)
    additional_tariff_rate: float = Field(ge=0)
    mpf_rate: float = Field(ge=0)
    brokerage_per_entry: float = Field(ge=0)
    units_per_shipment: int = Field(ge=0)
    categories: dict[str, CategoryCost]


class Settings(BaseModel):
    llm: LLMSettings
    pipeline: PipelineSettings = Field(default_factory=PipelineSettings)
    confidence: ConfidenceSettings = Field(default_factory=ConfidenceSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    data: DataSettings = Field(default_factory=DataSettings)
    tools: ToolSettings = Field(default_factory=ToolSettings)
    cost_model: CostModel
    config_dir: Path

    @property
    def db_path(self) -> Path:
        """Absolute path of the business SQLite database."""
        return resolve_path(self.storage.db_path)

    @property
    def checkpoint_path(self) -> Path:
        """Absolute path of the LangGraph checkpoint database (separate from ``db_path``)."""
        return resolve_path(self.storage.checkpoint_path)

    @property
    def upload_dir(self) -> Path:
        """Absolute path of the directory API uploads are saved in."""
        return resolve_path(self.storage.upload_dir)

    @property
    def dataset_dir(self) -> Path:
        """Absolute path of the active synthetic dataset."""
        return resolve_path(self.data.dataset_dir)


def resolve_path(path: Path) -> Path:
    """Resolve ``path`` against the project root unless it is absolute."""
    return path if path.is_absolute() else PROJECT_ROOT / path


def _pick(value: _T | None, default: _T) -> _T:
    return default if value is None else value


def _read_yaml(path: Path) -> dict[str, object]:
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ConfigError(f"Config file must contain a mapping: {path}")
    return data


def load_settings(config_dir: Path | None = None) -> Settings:
    """Load settings from ``config_dir`` (default: $RFQ_CONFIG_DIR or ``<root>/config``)."""
    if os.environ.get(ENV_SKIP_DOTENV) != "1":
        load_dotenv(PROJECT_ROOT / ".env", override=False)
    directory = config_dir or Path(os.environ.get(ENV_CONFIG_DIR) or DEFAULT_CONFIG_DIR)

    raw = _read_yaml(directory / "settings.yaml")
    raw["cost_model"] = _read_yaml(directory / "cost_model.yaml")
    raw["config_dir"] = directory

    forced = os.environ.get(ENV_FORCE_PROVIDER, "").strip()
    llm_raw = raw.get("llm")
    if forced and isinstance(llm_raw, dict):
        llm_raw["force_provider"] = forced

    return Settings.model_validate(raw)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide cached settings. Call ``get_settings.cache_clear()`` to reload."""
    return load_settings()
