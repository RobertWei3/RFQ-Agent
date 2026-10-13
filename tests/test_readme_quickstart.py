"""Spec 17 P11 acceptance: the README quickstart runs in a fresh copy of the repository.

The three commands are read from the README itself, so the test checks what readers run.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from rfq_agent.config import PROJECT_ROOT

QUICKSTART = re.compile(
    r"<!-- quickstart:begin -->\s*```bash\n(.*?)```\s*<!-- quickstart:end -->", re.DOTALL
)
# Variables a developer shell may carry that a fresh environment would not have.
SCRUBBED_ENV = (
    "VIRTUAL_ENV",
    "UV_PROJECT_ENVIRONMENT",
    "RFQ_LLM_PROVIDER",
    "RFQ_CONFIG_DIR",
    "DEEPSEEK_API_KEY",
    "ANTHROPIC_API_KEY",
    "DASHSCOPE_API_KEY",
)

pytestmark = pytest.mark.skipif(
    shutil.which("uv") is None or shutil.which("git") is None, reason="needs uv and git"
)


def quickstart_commands(readme: str) -> list[str]:
    """The shell commands of the README quickstart block (comments and blank lines dropped)."""
    match = QUICKSTART.search(readme)
    assert match, "README has no quickstart block"
    lines = (line.strip() for line in match.group(1).splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _fresh_copy(target: Path) -> Path:
    """What a clone would contain once the working tree is committed (ignored files excluded)."""
    listed = _git(PROJECT_ROOT, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    for name in filter(None, listed.split("\0")):
        source = PROJECT_ROOT / name
        if source.is_file():  # deleted but not yet committed files are skipped
            (target / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target / name)
    _git(target, "init", "-q")
    _git(target, "add", "-A")
    _git(target, "commit", "-q", "-m", "fresh copy")
    return target


def test_the_english_and_chinese_readmes_share_the_quickstart() -> None:
    english = quickstart_commands((PROJECT_ROOT / "README.md").read_text(encoding="utf-8"))
    chinese = quickstart_commands((PROJECT_ROOT / "README.zh-CN.md").read_text(encoding="utf-8"))
    assert chinese == english


def test_the_readme_quickstart_runs_in_a_fresh_copy(tmp_path: Path) -> None:
    commands = quickstart_commands((PROJECT_ROOT / "README.md").read_text(encoding="utf-8"))
    assert len(commands) == 3, commands
    repo = _fresh_copy(tmp_path / "RFQ-Agent")
    env = {k: v for k, v in os.environ.items() if k not in SCRUBBED_ENV}

    outputs: list[str] = []
    for command in commands:
        result = subprocess.run(
            ["bash", "-c", command],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
        )
        assert result.returncode == 0, f"{command}\n{result.stdout}\n{result.stderr}"
        outputs.append(result.stdout + result.stderr)

    assert (repo / "data" / "generated" / "n200_s42" / "internal.db").is_file()
    match = re.search(r"run_id=(\S+)", outputs[-1])
    assert match, outputs[-1]
    for suffix in (".xlsx", ".jsonl"):
        assert (repo / "data" / "runs" / f"run_{match.group(1)}{suffix}").is_file()
    assert "row correct 20/20" in outputs[-1]
    assert _git(repo, "status", "--porcelain") == ""
