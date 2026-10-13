from __future__ import annotations

import pytest
from typer.testing import CliRunner

from rfq_agent.cli import app

runner = CliRunner()


def test_help_lists_command_groups() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "config" in result.output and "llm" in result.output


def test_config_show_masks_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RFQ_LLM_PROVIDER")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-super-secret")
    result = runner.invoke(app, ["config", "show"])
    assert result.exit_code == 0
    assert "sk-super-secret" not in result.output
    assert "api_key: set" in result.output
    assert "deepseek-v4-pro" in result.output


def test_llm_ping_fake() -> None:
    result = runner.invoke(app, ["llm", "ping", "--role", "judge"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("OK provider=fake")


def test_llm_ping_refuses_real_provider_without_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RFQ_LLM_PROVIDER")
    result = runner.invoke(app, ["llm", "ping"])
    assert result.exit_code == 2
    assert "--allow-real" in result.output


def test_data_generate_and_stats(tmp_path) -> None:  # type: ignore[no-untyped-def]
    out = tmp_path / "ds"
    sample = tmp_path / "sample.xlsx"
    result = runner.invoke(
        app,
        [
            "data",
            "generate",
            "--n-parts",
            "60",
            "--seed",
            "1",
            "--out",
            str(out),
            "--sample-out",
            str(sample),
            "--ratio",
            "dirty_input=0.1",
        ],
    )
    assert result.exit_code == 0, result.output
    assert (out / "rfq_60.xlsx").is_file() and sample.is_file()
    dirty_line = next(line for line in result.output.splitlines() if line.startswith("dirty_input"))
    assert dirty_line.split()[1:4] == ["10.0%", "6", "10.0%"]

    stats = runner.invoke(app, ["data", "stats", "--dataset", str(out)])
    assert stats.exit_code == 0
    assert "measured" in stats.output


def test_data_generate_rejects_bad_ratio(tmp_path) -> None:  # type: ignore[no-untyped-def]
    result = runner.invoke(
        app, ["data", "generate", "--out", str(tmp_path), "--ratio", "nonsense=0.1"]
    )
    assert result.exit_code != 0


def test_tools_search(generated_dataset, config_dir) -> None:  # type: ignore[no-untyped-def]
    import yaml

    path = config_dir / "settings.yaml"
    data = yaml.safe_load(path.read_text())
    data["data"]["dataset_dir"] = str(generated_dataset)
    path.write_text(yaml.safe_dump(data))

    result = runner.invoke(app, ["tools", "search", "msrp_lookup", "zz-none"])
    assert result.exit_code == 0, result.output
    bad = runner.invoke(app, ["tools", "search", "nope", "x"])
    assert bad.exit_code != 0
