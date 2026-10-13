"""Which ground truth belongs to a run (spec 14, ``rfq eval`` without ``--truth``)."""

from __future__ import annotations

from pathlib import Path

import pytest

from rfq_agent import config
from rfq_agent.eval.run_eval import find_truth
from rfq_agent.schemas import RunSpec


@pytest.fixture
def dataset(tmp_path: Path) -> Path:
    root = tmp_path / "data" / "generated" / "n200_s42"
    root.mkdir(parents=True)
    for name in ("rfq_200.xlsx", "ground_truth.jsonl", "rfq_sample_20.xlsx"):
        (root / name).write_text("x")
    (root / "rfq_sample_20_ground_truth.jsonl").write_text("x")
    return root


def spec(input: str, dataset_dir: str) -> RunSpec:
    return RunSpec(
        input=input, dataset_dir=dataset_dir, batch_size=100, strategy="evidence", review=False
    )


def test_the_dataset_workbook_uses_the_dataset_truth(dataset: Path) -> None:
    found = find_truth(spec(str(dataset / "rfq_200.xlsx"), str(dataset)))
    assert found == dataset / "ground_truth.jsonl"


def test_a_sample_workbook_uses_its_own_truth(dataset: Path, tmp_path: Path) -> None:
    upload = tmp_path / "uploads" / "0123456789ab_rfq_sample_20.xlsx"
    found = find_truth(spec(str(upload), str(dataset)))
    assert found == dataset / "rfq_sample_20_ground_truth.jsonl"


def test_an_uploaded_dataset_workbook_uses_the_dataset_truth(dataset: Path, tmp_path: Path) -> None:
    upload = tmp_path / "uploads" / "0123456789ab_rfq_200.xlsx"
    found = find_truth(spec(str(upload), str(dataset)))
    assert found == dataset / "ground_truth.jsonl"


def test_relative_and_absolute_paths_are_compared_resolved(
    dataset: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Relative settings paths resolve against the project root (config.resolve_path).
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    found = find_truth(spec(str(dataset / "rfq_200.xlsx"), "data/generated/n200_s42"))
    assert found == dataset / "ground_truth.jsonl"


def test_an_unknown_workbook_has_no_truth(dataset: Path, tmp_path: Path) -> None:
    assert find_truth(spec(str(tmp_path / "my_rfq.xlsx"), str(dataset))) is None
