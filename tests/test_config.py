from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from rfq_agent.config import ConfigError, load_settings


def test_loads_project_config(settings) -> None:  # type: ignore[no-untyped-def]
    assert settings.pipeline.batch_size == 100
    assert settings.pipeline.structured_output_retries == 2
    assert settings.confidence.strategy == "evidence_plus_judge"  # P10 injection experiment
    assert settings.confidence.bands.green_min == 80
    assert settings.llm.max_concurrency == 8
    assert set(settings.cost_model.categories) == {"thermostat", "water_pump"}


def test_fake_env_forces_every_role(settings) -> None:  # type: ignore[no-untyped-def]
    assert settings.llm.for_role("default").provider == "fake"
    assert settings.llm.for_role("judge").provider == "fake"
    assert settings.llm.for_role("judge").api_key_env is None


@pytest.mark.usefixtures("real_provider_env")
def test_role_resolution_without_override(config_dir: Path) -> None:
    settings = load_settings(config_dir)
    default = settings.llm.for_role("default")
    judge = settings.llm.for_role("judge")
    unknown = settings.llm.for_role("oe_interchange")

    assert (default.provider, default.model) == ("deepseek", "deepseek-flash")
    assert default.base_url == "https://api.deepseek.com"
    assert default.thinking is False
    assert default.pricing is not None and default.pricing.output == 1.20
    assert judge.model == "deepseek-v4-pro"
    assert judge.pricing is not None and judge.pricing.input == 1.32
    assert unknown == default.model_copy(update={"role": "oe_interchange"})


@pytest.mark.usefixtures("real_provider_env")
def test_role_switching_provider_uses_provider_defaults(config_dir: Path) -> None:
    path = config_dir / "settings.yaml"
    data = yaml.safe_load(path.read_text())
    data["llm"]["roles"]["judge"] = {"provider": "anthropic"}
    data["llm"]["roles"]["qwen_role"] = {"provider": "qwen"}
    path.write_text(yaml.safe_dump(data))

    settings = load_settings(config_dir)
    judge = settings.llm.for_role("judge")
    qwen = settings.llm.for_role("qwen_role")

    assert (judge.model, judge.base_url, judge.api_key_env) == (
        "claude-sonnet-5-5",
        None,
        "ANTHROPIC_API_KEY",
    )
    assert judge.pricing is None
    assert qwen.model == "qwen-plus"
    assert qwen.base_url is not None and "dashscope" in qwen.base_url


def test_missing_config_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_settings(tmp_path)


def test_relative_db_path_resolves_to_project_root(config_dir: Path) -> None:
    path = config_dir / "settings.yaml"
    data = yaml.safe_load(path.read_text())
    data["storage"]["db_path"] = "data/rfq.db"
    path.write_text(yaml.safe_dump(data))

    settings = load_settings(config_dir)
    assert settings.db_path.is_absolute()
    assert settings.db_path.parts[-2:] == ("data", "rfq.db")


def test_invalid_band_order_rejected(config_dir: Path) -> None:
    path = config_dir / "settings.yaml"
    data = yaml.safe_load(path.read_text())
    data["confidence"]["bands"] = {"green_min": 40, "yellow_min": 60}
    path.write_text(yaml.safe_dump(data))

    with pytest.raises(ValueError, match="yellow_min"):
        load_settings(config_dir)


def test_cost_model_disclaimer_present() -> None:
    from rfq_agent.config import DEFAULT_CONFIG_DIR

    first_line = (
        (DEFAULT_CONFIG_DIR / "cost_model.yaml").read_text(encoding="utf-8").splitlines()[0]
    )
    assert "示意值" in first_line


def test_dotenv_ignored_when_skip_flag_set(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    from rfq_agent import config as config_mod

    env_file = config_mod.PROJECT_ROOT / ".env"
    if not env_file.is_file():
        pytest.skip("no project .env to leak")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    load_settings(config_dir)
    assert "DEEPSEEK_API_KEY" not in os.environ


@pytest.mark.parametrize("field", ["oe_match", "spec_source"])
def test_evidence_levels_must_name_every_known_value(field: str) -> None:
    from rfq_agent.config import EvidenceSettings

    levels = dict(getattr(EvidenceSettings(), field))
    levels["typo"] = levels.pop(next(iter(levels)))
    with pytest.raises(ValueError, match=field):
        EvidenceSettings(**{field: levels})


@pytest.mark.parametrize(("green", "yellow"), [(50, 50), (80, 0)])
def test_bands_need_distinct_positive_thresholds(green: int, yellow: int) -> None:
    from rfq_agent.config import BandSettings

    with pytest.raises(ValueError, match="yellow_min"):
        BandSettings(green_min=green, yellow_min=yellow)


def test_checkpoints_live_in_their_own_file(settings) -> None:  # type: ignore[no-untyped-def]
    assert settings.checkpoint_path.name == "checkpoints.db"
    assert settings.checkpoint_path != settings.db_path
    assert settings.confidence.evidence.oe_match["manual"] == 1.0


def test_a_lease_must_outlive_two_heartbeats(config_dir: Path) -> None:
    # A heartbeat retries a failed refresh only while one more interval and one lock wait
    # (at most an interval) still fit in the TTL.
    path = config_dir / "settings.yaml"
    data = yaml.safe_load(path.read_text())
    data["storage"].update(lease_ttl_s=20, lease_heartbeat_s=10)
    path.write_text(yaml.safe_dump(data))

    with pytest.raises(ValueError, match="lease_ttl_s"):
        load_settings(config_dir)
