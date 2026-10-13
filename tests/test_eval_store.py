"""Result store for evaluation (spec 14): stored evaluations, usage by agent, rescore copies."""

from __future__ import annotations

from pathlib import Path

import pytest

from rfq_agent.eval.run_eval import lineage_usage
from rfq_agent.schemas import ReviewEntry, RunEval, RunSpec
from rfq_agent.store.db import connect
from rfq_agent.store.runs import RescoreRefused, RowOutcomes, RunStore
from tests.builders import cost, line, msrp, oe, resolved_row, retail, run_eval


def _spec() -> RunSpec:
    return RunSpec(
        input="rfq.xlsx", dataset_dir="data/x", batch_size=2, strategy="evidence", review=False
    )


def _outcomes(row_id: int) -> RowOutcomes:
    return RowOutcomes(
        row_id=row_id,
        oe=oe(row_id),
        msrp=msrp(row_id),
        retail=retail(row_id),
        should_cost=cost(row_id),
    )


def _finished_run(store: RunStore) -> str:
    run_id = store.create_run(_spec(), [line(1), line(2), line(3)])
    store.save_batch(run_id, 1, [_outcomes(1), _outcomes(2)])
    store.fail_batch(run_id, 2, "RuntimeError: mcp down")
    store.mark_finalized(run_id, "completed")
    return run_id


def test_an_evaluation_is_stored_per_run_and_replaced_on_rerun(tmp_path: Path) -> None:
    store = RunStore.open(tmp_path / "rfq.db")
    assert store.evaluation("run-a") is None
    store.save_evaluation(run_eval("run-a"))
    again = run_eval("run-a").model_copy(update={"created_at": "2026-10-10T00:00:00+00:00"})
    store.save_evaluation(again)
    stored = store.evaluation("run-a")
    assert isinstance(stored, RunEval) and stored == again


def test_usage_is_grouped_by_agent_with_the_models_per_role(tmp_path: Path) -> None:
    store = RunStore.open(tmp_path / "rfq.db")
    conn = connect(tmp_path / "rfq.db")
    with conn:
        conn.executemany(
            "INSERT INTO llm_calls (ts, rfq_run_id, agent, role, provider, model, input_tokens, "
            "output_tokens, cost_usd, status) VALUES ('t', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("r", "oe_interchange", "oe_interchange", "deepseek", "flash", 10, 1, 0.1, "ok"),
                ("r", "oe_interchange", "oe_interchange", "deepseek", "flash", 20, 2, 0.2, "ok"),
                ("r", "oe_interchange", "oe_interchange", "deepseek", "flash", 5, 0, None, "error"),
                ("r", "confidence_judge", "judge", "deepseek", "pro", 7, 3, None, "ok"),
                ("other", "oe_msrp", "oe_msrp", "deepseek", "flash", 1, 1, 9.0, "ok"),
            ],
        )
    conn.close()

    by_agent = {a.agent: a for a in store.agent_usage("r")}
    assert set(by_agent) == {"oe_interchange", "confidence_judge"}
    oe_calls = by_agent["oe_interchange"]
    assert (oe_calls.calls, oe_calls.errors, oe_calls.input_tokens, oe_calls.output_tokens) == (
        3,
        1,
        35,
        3,
    )
    assert oe_calls.cost_usd == pytest.approx(0.3)
    assert by_agent["confidence_judge"].cost_usd is None  # an unpriced successful call
    assert store.llm_models("r") == {
        "oe_interchange": "deepseek/flash",
        "judge": "deepseek/pro",
    }


def test_rescore_copies_lines_batches_and_outcomes_into_a_new_run(tmp_path: Path) -> None:
    store = RunStore.open(tmp_path / "rfq.db")
    source = _finished_run(store)

    copy = store.copy_for_rescore(source, "evidence_plus_judge")

    record = store.run(copy)
    assert record.status == "running" and copy != source
    assert record.spec.strategy == "evidence_plus_judge"
    assert record.spec.rescored_from == source and record.spec.review is False
    assert record.spec.input == "rfq.xlsx" and record.spec.batch_size == 2
    assert store.lines(copy) == store.lines(source)
    assert store.outcomes(copy) == store.outcomes(source)
    assert [(b.batch_id, b.row_ids, b.status, b.error) for b in store.batches(copy)] == [
        (1, [1, 2], "completed", None),
        (2, [3], "failed", "RuntimeError: mcp down"),
    ]


def test_rescore_needs_a_finalized_source(tmp_path: Path) -> None:
    store = RunStore.open(tmp_path / "rfq.db")
    running = store.create_run(_spec(), [line(1)])
    with pytest.raises(RescoreRefused, match="not finalized"):
        store.copy_for_rescore(running, "evidence")
    with pytest.raises(KeyError):
        store.copy_for_rescore("no-such-run", "evidence")


def test_rescore_refuses_a_source_with_human_reviews(tmp_path: Path) -> None:
    store = RunStore.open(tmp_path / "rfq.db")
    source = _finished_run(store)
    store.log_review(
        source, ReviewEntry(row_id=1, action="accept", snapshot=resolved_row(1, "red"))
    )
    with pytest.raises(RescoreRefused, match="review"):
        store.copy_for_rescore(source, "evidence")


def test_rescore_copies_outcomes_in_a_database_whose_columns_were_added_later(
    tmp_path: Path,
) -> None:
    # A result store created before msrp_from / retail_from existed: opening it appends
    # them at the end of row_outcomes, so the column order differs from a new database.
    db = tmp_path / "old.db"
    conn = connect(db)
    conn.executescript(
        """
        CREATE TABLE row_outcomes (
            run_id TEXT NOT NULL, row_id INTEGER NOT NULL, batch_id INTEGER NOT NULL,
            price_key TEXT, msrp_ok INTEGER NOT NULL, retail_ok INTEGER NOT NULL,
            any_failure INTEGER NOT NULL, manual_oe INTEGER NOT NULL DEFAULT 0,
            data TEXT NOT NULL, PRIMARY KEY (run_id, row_id)
        );
        """
    )
    conn.close()
    store = RunStore.open(db)
    source = _finished_run(store)

    copy = store.copy_for_rescore(source, "evidence")

    assert store.outcomes(copy) == store.outcomes(source)


def test_injections_are_stored_per_run_and_copied_by_rescore(tmp_path: Path) -> None:
    store = RunStore.open(tmp_path / "rfq.db")
    source = _finished_run(store)
    assert store.injections(source) == {}
    injected = store.copy_for_injection(source, [], {2: "msrp", 1: "oe"})
    store.mark_finalized(injected, "completed")

    copy = store.copy_for_rescore(injected, "evidence_plus_judge")

    assert store.injections(source) == {}
    assert store.injections(injected) == {1: "oe", 2: "msrp"}
    assert store.injections(copy) == {1: "oe", 2: "msrp"}


def test_the_cost_of_a_run_includes_every_run_it_was_copied_from(tmp_path: Path) -> None:
    store = RunStore.open(tmp_path / "rfq.db")
    agents = _finished_run(store)  # ran the stage agents
    injected = store.copy_for_rescore(agents, "evidence")  # no LLM call
    store.mark_finalized(injected, "completed")
    rescored = store.copy_for_rescore(injected, "evidence_plus_judge")
    conn = connect(tmp_path / "rfq.db")
    with conn:
        conn.executemany(
            "INSERT INTO llm_calls (ts, rfq_run_id, agent, cost_usd, status) "
            "VALUES ('t', ?, ?, ?, 'ok')",
            [(agents, "oe_interchange", 0.4), (rescored, "confidence_judge", 0.1)],
        )
    conn.close()

    assert lineage_usage(store, agents) is None
    [from_agents] = lineage_usage(store, injected) or []
    assert (from_agents.agent, from_agents.cost_usd) == ("oe_interchange", 0.4)
    assert [a.cost_usd for a in lineage_usage(store, rescored) or []] == [0.4]


def test_an_injection_copy_is_all_or_nothing(tmp_path: Path) -> None:
    store = RunStore.open(tmp_path / "rfq.db")
    source = _finished_run(store)
    before = [r.run_id for r in store.list_runs()]

    with pytest.raises(KeyError):
        store.copy_for_injection(source, [_outcomes(1)], {99: "oe"})  # row 99 is not in the run

    assert [r.run_id for r in store.list_runs()] == before

    copy = store.copy_for_injection(source, [_outcomes(1)], {1: "oe"})
    assert store.run(copy).spec.rescored_from == source
    assert store.run(copy).spec.strategy == "evidence"
    assert store.injections(copy) == {1: "oe"}
    assert store.outcomes(copy) == store.outcomes(source)
