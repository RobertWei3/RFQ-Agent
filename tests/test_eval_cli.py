"""``rfq eval``, ``rfq eval compare`` and ``rfq rescore`` (spec 13, 14), fake mode."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from rfq_agent.cli import app
from rfq_agent.config import load_settings
from rfq_agent.schemas import ReviewEntry, RunSpec
from rfq_agent.store.runs import RunStore
from tests.builders import line


@pytest.fixture
def cli_config(config_dir: Path, generated_dataset: Path) -> Path:
    path = config_dir / "settings.yaml"
    data = yaml.safe_load(path.read_text())
    data["data"]["dataset_dir"] = str(generated_dataset)
    path.write_text(yaml.safe_dump(data))
    return config_dir


def _run_id(output: str) -> str:
    found = re.findall(r"run_id=(\S+)", output)
    assert found, output
    return found[-1]


def _store(config_dir: Path) -> RunStore:
    return RunStore.open(load_settings(config_dir).db_path)


def _run(workbook: Path, tmp_path: Path, *extra: str) -> str:
    result = CliRunner().invoke(
        app, ["run", "--input", str(workbook), "--output-dir", str(tmp_path / "runs"), *extra]
    )
    assert result.exit_code == 0, result.output
    return _run_id(result.output)


def _uploaded_sample(generated_dataset: Path, tmp_path: Path) -> Path:
    """The 20-row sample as the API stores an upload: ``<uuid12>_<name>`` in another dir."""
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    target = uploads / "0123456789ab_rfq_sample_20.xlsx"
    shutil.copy(generated_dataset / "rfq_sample_20.xlsx", target)
    return target


def test_eval_writes_the_report_and_stores_the_evaluation(
    cli_config: Path, generated_dataset: Path, tmp_path: Path
) -> None:
    run_id = _run(_uploaded_sample(generated_dataset, tmp_path), tmp_path)

    result = CliRunner().invoke(app, ["eval", "--run", run_id, "--out", str(tmp_path / "rep")])

    assert result.exit_code == 0, result.output
    report = tmp_path / "rep" / f"eval_{run_id}.md"
    assert report.is_file()
    assert (tmp_path / "rep" / f"eval_{run_id}_reliability.png").is_file()
    assert (tmp_path / "rep" / f"eval_{run_id}_coverage.png").is_file()
    assert "rfq_sample_20_ground_truth.jsonl" in report.read_text()
    assert "ECE" in result.output and "AUROC" in result.output
    stored = _store(cli_config).evaluation(run_id)
    assert stored is not None and stored.rows == 20 and len(stored.row_evals) == 20
    assert stored.reviewed_rows == 0


def test_eval_refuses_ground_truth_of_another_rfq(
    cli_config: Path, generated_dataset: Path, tmp_path: Path
) -> None:
    run_id = _run(generated_dataset / "rfq_sample_20.xlsx", tmp_path)
    wrong = generated_dataset / "ground_truth.jsonl"  # the 200-row truth

    result = CliRunner().invoke(app, ["eval", "--run", run_id, "--truth", str(wrong)])

    assert result.exit_code != 0
    assert "ground truth" in result.output
    assert _store(cli_config).evaluation(run_id) is None


def test_eval_asks_for_truth_when_it_cannot_be_inferred(
    cli_config: Path, generated_dataset: Path, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "my_rfq.xlsx"
    shutil.copy(generated_dataset / "rfq_sample_20.xlsx", elsewhere)
    run_id = _run(elsewhere, tmp_path)

    result = CliRunner().invoke(app, ["eval", "--run", run_id])

    assert result.exit_code != 0
    assert "--truth" in result.output


def test_eval_judges_reviewed_rows_by_their_pre_review_result(
    cli_config: Path, generated_dataset: Path, tmp_path: Path
) -> None:
    sample = generated_dataset / "rfq_sample_20.xlsx"
    run_id = _run(sample, tmp_path)
    first = CliRunner().invoke(app, ["eval", "--run", run_id, "--out", str(tmp_path / "a")])
    assert first.exit_code == 0, first.output
    before = _store(cli_config).evaluation(run_id)
    assert before is not None
    red = next(r for r in before.row_evals if r.band == "red")

    # A person changes the price of a red row: the evaluation must not move.
    store = _store(cli_config)
    [row] = store.rows(run_id, [red.row_id])
    sheet = row.worksheet.model_copy(update={"msrp": 1.0, "review_status": "modified"})
    store.log_review(
        run_id, ReviewEntry(row_id=red.row_id, action="modify_price", msrp_after=1.0, snapshot=row)
    )

    [stored_row] = store.stored_rows(run_id, [red.row_id])
    store.save_rows(
        run_id,
        [stored_row.model_copy(update={"row": row.model_copy(update={"worksheet": sheet})})],
    )
    store.close()

    again = CliRunner().invoke(app, ["eval", "--run", run_id, "--out", str(tmp_path / "b")])

    assert again.exit_code == 0, again.output
    after = _store(cli_config).evaluation(run_id)
    assert after is not None and after.reviewed_rows == 1
    assert after.row_evals == before.row_evals
    assert after.msrp == before.msrp


def test_rescore_reuses_the_stage_outcomes_and_only_rescores(
    cli_config: Path, generated_dataset: Path, tmp_path: Path
) -> None:
    source = _run(generated_dataset / "rfq_sample_20.xlsx", tmp_path)

    result = CliRunner().invoke(
        app,
        ["rescore", source, "--strategy", "evidence_plus_judge", "--output-dir", str(tmp_path)],
    )

    assert result.exit_code == 0, result.output
    # judge reviews every row without a red gate: green 11 + yellow 5 of the sample
    assert "LLM calls: judge" in result.output and "expected 16" in result.output
    copy = _run_id(result.output)
    store = _store(cli_config)
    record = store.run(copy)
    assert record.status == "completed" and record.spec.rescored_from == source
    assert record.spec.strategy == "evidence_plus_judge"
    assert [a.agent for a in store.agent_usage(copy)] == ["confidence_judge"]
    assert [a.calls for a in store.agent_usage(copy)] == [16]
    assert [r.line for r in store.rows(copy)] == [r.line for r in store.rows(source)]
    assert {r.worksheet.band for r in store.rows(copy)} <= {"green", "yellow", "red"}


def test_rescore_refuses_a_run_that_is_not_finalized(
    cli_config: Path, generated_dataset: Path
) -> None:
    store = _store(cli_config)

    spec = RunSpec(input="x.xlsx", dataset_dir=".", batch_size=5, strategy="evidence", review=False)
    running = store.create_run(spec, [line(1)])
    store.close()

    result = CliRunner().invoke(app, ["rescore", running, "--strategy", "evidence"])

    assert result.exit_code != 0 and "not finalized" in result.output


def test_compare_prints_one_line_per_run_and_writes_the_report(
    cli_config: Path, generated_dataset: Path, tmp_path: Path
) -> None:
    source = _run(generated_dataset / "rfq_sample_20.xlsx", tmp_path, "--strategy", "evidence")
    rescored = CliRunner().invoke(
        app, ["rescore", source, "--strategy", "evidence_plus_self", "--output-dir", str(tmp_path)]
    )
    copy = _run_id(rescored.output)
    reports = tmp_path / "rep"
    for run_id in (source, copy):
        done = CliRunner().invoke(app, ["eval", "--run", run_id, "--out", str(reports)])
        assert done.exit_code == 0, done.output

    result = CliRunner().invoke(
        app, ["eval", "compare", "--runs", source, copy, "--out", str(reports)]
    )

    assert result.exit_code == 0, result.output
    assert f"| {source} | evidence |" in result.output
    assert f"| {copy} | evidence_plus_self |" in result.output
    assert len(list(reports.glob("compare_*.md"))) == 1
    assert len(list(reports.glob("compare_*.png"))) == 2


def test_compare_needs_every_run_evaluated_on_the_same_rows(
    cli_config: Path, generated_dataset: Path, tmp_path: Path
) -> None:
    whole = _run(generated_dataset / "rfq_sample_20.xlsx", tmp_path)
    part = _run(generated_dataset / "rfq_sample_20.xlsx", tmp_path, "--limit", "10")
    reports = str(tmp_path / "rep")

    missing = CliRunner().invoke(app, ["eval", "compare", "--runs", whole, part])
    assert missing.exit_code != 0 and "rfq eval --run" in missing.output

    for run_id in (whole, part):
        CliRunner().invoke(app, ["eval", "--run", run_id, "--out", reports])
    differ = CliRunner().invoke(app, ["eval", "compare", "--runs", whole, part, "--out", reports])
    assert differ.exit_code != 0 and "same" in differ.output


def test_inject_makes_a_finalized_copy_with_known_wrong_rows(
    cli_config: Path, generated_dataset: Path, tmp_path: Path
) -> None:
    source = _run(generated_dataset / "rfq_sample_20.xlsx", tmp_path)

    result = CliRunner().invoke(
        app,
        ["eval", "inject", source, "--rows", "3", "--seed", "7", "--output-dir", str(tmp_path)],
    )

    assert result.exit_code == 0, result.output
    assert "injected 3 rows: oe 1, msrp 1, retail 1" in result.output
    injected = _run_id(result.output)
    store = _store(cli_config)
    record = store.run(injected)
    assert record.status == "completed" and record.spec.rescored_from == source
    assert record.spec.strategy == "evidence"
    assert sorted(store.injections(injected).values()) == ["msrp", "oe", "retail"]
    assert store.agent_usage(injected) == []  # evidence: no LLM call
    store.close()

    rescored = CliRunner().invoke(
        app,
        ["rescore", injected, "--strategy", "evidence_plus_judge", "--output-dir", str(tmp_path)],
    )
    assert rescored.exit_code == 0, rescored.output
    copy = _run_id(rescored.output)
    reports = tmp_path / "rep"
    for run_id in (injected, copy):
        done = CliRunner().invoke(app, ["eval", "--run", run_id, "--out", str(reports)])
        assert done.exit_code == 0, done.output
        assert "injected 3 rows" in done.output
    evaluation = _store(cli_config).evaluation(copy)
    assert evaluation is not None and evaluation.injection is not None
    assert evaluation.injection.rows == 3
    assert "## Injected errors" in (reports / f"eval_{copy}.md").read_text()

    compared = CliRunner().invoke(
        app, ["eval", "compare", "--runs", injected, copy, "--out", str(reports)]
    )
    assert compared.exit_code == 0, compared.output
    assert "injected held back" in compared.output


def test_inject_refuses_more_rows_than_can_take_an_error(
    cli_config: Path, generated_dataset: Path, tmp_path: Path
) -> None:
    source = _run(generated_dataset / "rfq_sample_20.xlsx", tmp_path)
    result = CliRunner().invoke(app, ["eval", "inject", source, "--rows", "20"])
    assert result.exit_code != 0 and "injectable rows" in result.output
