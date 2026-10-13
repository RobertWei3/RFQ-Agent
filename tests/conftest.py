"""Shared fixtures. Every test runs in fake mode against a temporary config + DB."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import mcp.client.stdio  # noqa: F401 - see note below
import pytest
import yaml

from rfq_agent.config import DEFAULT_CONFIG_DIR, Settings, get_settings, load_settings
from rfq_agent.data_gen.params import GenParams
from rfq_agent.data_gen.writer import generate_dataset

# mcp's stdio_client binds ``errlog=sys.stderr`` when the module is first imported. If that
# first import happens inside typer's CliRunner, it captures CliRunner's stderr stand-in,
# which has no fileno(), and spawning the MCP server fails. Importing it here, before any
# CliRunner, binds the real stderr. Real CLI runs are unaffected.

API_KEY_ENVS = ("DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "DASHSCOPE_API_KEY")


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """Copy of the real config with the database redirected into ``tmp_path``."""
    target = tmp_path / "config"
    shutil.copytree(DEFAULT_CONFIG_DIR, target)
    settings_file = target / "settings.yaml"
    data = yaml.safe_load(settings_file.read_text(encoding="utf-8"))
    data["storage"]["db_path"] = str(tmp_path / "rfq.db")
    data["storage"]["checkpoint_path"] = str(tmp_path / "checkpoints.db")
    data["storage"]["upload_dir"] = str(tmp_path / "uploads")
    settings_file.write_text(yaml.safe_dump(data), encoding="utf-8")
    return target


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, config_dir: Path) -> Iterator[None]:
    """Force fake mode, ignore the project .env, drop real keys, use the temp config copy."""
    monkeypatch.setenv("RFQ_SKIP_DOTENV", "1")
    monkeypatch.setenv("RFQ_LLM_PROVIDER", "fake")
    monkeypatch.setenv("RFQ_CONFIG_DIR", str(config_dir))
    for name in API_KEY_ENVS:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def settings(config_dir: Path) -> Settings:
    return load_settings(config_dir)


@pytest.fixture
def real_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable the fake override so real-provider resolution can be tested (no calls made)."""
    monkeypatch.delenv("RFQ_LLM_PROVIDER", raising=False)
    get_settings.cache_clear()


@pytest.fixture(scope="session")
def generated_dataset(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The default 200-row / seed 42 dataset, generated once per test session."""
    out = tmp_path_factory.mktemp("datasets") / "n200_s42"
    cost_model = load_settings(DEFAULT_CONFIG_DIR).cost_model
    generate_dataset(GenParams(n_parts=200, seed=42), cost_model, out, out / "sample_copy.xlsx")
    return out
