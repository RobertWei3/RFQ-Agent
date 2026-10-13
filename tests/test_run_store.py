"""Result store for orchestrated runs (spec 13, phase-7 Q10)."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from rfq_agent.schemas import RowFailure, RunSpec
from rfq_agent.store.db import check_sqlite_version, connect
from rfq_agent.store.runs import LeaseHeld, RowOutcomes, RunStore
from tests.builders import cost, line, msrp, oe, retail


def _spec(batch_size: int = 2) -> RunSpec:
    return RunSpec(
        input="rfq.xlsx",
        dataset_dir="data/generated/x",
        batch_size=batch_size,
        strategy="evidence",
        review=False,
    )


def _outcomes(row_id: int, current: str | None = "NEW-3") -> RowOutcomes:
    return RowOutcomes(
        row_id=row_id,
        oe=oe(row_id, current),
        msrp=msrp(row_id),
        retail=retail(row_id),
        should_cost=cost(row_id),
    )


def _store(tmp_path: Path) -> RunStore:
    return RunStore.open(tmp_path / "rfq.db")


def test_a_new_run_splits_its_lines_into_pending_batches_in_row_order(tmp_path: Path) -> None:
    store = _store(tmp_path)

    run_id = store.create_run(_spec(batch_size=2), [line(3), line(1), line(2)])

    batches = store.batches(run_id)
    assert [(b.batch_id, b.row_ids, b.status, b.attempts) for b in batches] == [
        (1, [1, 2], "pending", 0),
        (2, [3], "pending", 0),
    ]
    assert [x.row_id for x in store.lines(run_id, [3, 1])] == [1, 3]
    assert store.run(run_id).spec == _spec(batch_size=2)


def test_saving_a_batch_completes_it_and_keeps_failures(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run_id = store.create_run(_spec(), [line(1), line(2)])
    failed = RowOutcomes(
        row_id=2,
        oe=RowFailure(row_id=2, stage="search", error="timeout"),
        msrp=RowFailure(row_id=2, stage="upstream_oe", error="x"),
        retail=RowFailure(row_id=2, stage="upstream_oe", error="x"),
        should_cost=RowFailure(row_id=2, stage="upstream_oe", error="x"),
    )

    store.start_batch(run_id, 1)
    store.save_batch(run_id, 1, [_outcomes(1), failed])

    assert store.batches(run_id)[0].status == "completed"
    assert store.batches(run_id)[0].attempts == 1
    assert store.outcomes(run_id) == [_outcomes(1), failed]


def test_a_failed_batch_records_its_error_and_counts_attempts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run_id = store.create_run(_spec(), [line(1)])

    store.start_batch(run_id, 1)
    store.fail_batch(run_id, 1, "RuntimeError: mcp down")
    store.start_batch(run_id, 1)

    batch = store.batches(run_id)[0]
    assert (batch.status, batch.attempts, batch.error) == ("running", 2, "RuntimeError: mcp down")


def test_cached_prices_are_own_non_failure_results_outside_open_batches(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    run_id = store.create_run(_spec(batch_size=2), [line(i) for i in range(1, 7)])
    msrp_failed = _outcomes(1).model_copy(
        update={"msrp": RowFailure(row_id=1, stage="search_chain", error="timeout")}
    )
    copy = _outcomes(5, "OTHER-9").model_copy(update={"msrp_from": 3, "retail_from": 3})
    for batch, rows in ((1, [msrp_failed, _outcomes(2)]), (2, [_outcomes(3, "OTHER-9")])):
        store.start_batch(run_id, batch)
        store.save_batch(run_id, batch, rows)
    store.start_batch(run_id, 3)
    store.save_batch(run_id, 3, [copy, _outcomes(6, "OTHER-9")])

    store.reopen_for_rerun(run_id, {2: [3]})  # row 3 is about to run again
    cached = store.cached_prices(run_id, {"NEW3", "OTHER9"})

    assert {k: v.row_id for k, v in cached.msrp.items()} == {"NEW3": 2, "OTHER9": 6}
    assert {k: v.row_id for k, v in cached.retail.items()} == {"NEW3": 1, "OTHER9": 6}


def test_a_rerun_plan_reopens_the_run_and_its_batches(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run_id = store.create_run(_spec(batch_size=2), [line(i) for i in range(1, 5)])
    for batch, rows in ((1, [_outcomes(1), _outcomes(2)]), (2, [_outcomes(3), _outcomes(4)])):
        store.start_batch(run_id, batch)
        store.save_batch(run_id, batch, rows)
    store.set_run_status(run_id, "completed")

    store.reopen_for_rerun(run_id, {2: [4]})

    assert store.run(run_id).status == "running"
    assert [(b.status, b.rerun_rows) for b in store.batches(run_id)] == [
        ("completed", None),
        ("pending", [4]),
    ]
    store.start_batch(run_id, 2)
    store.save_batch(run_id, 2, [_outcomes(4)])
    assert store.batches(run_id)[1].rerun_rows is None


def test_rows_to_rerun_are_failed_batches_plus_rows_with_a_failure(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run_id = store.create_run(_spec(batch_size=2), [line(i) for i in range(1, 6)])
    store.start_batch(run_id, 1)
    store.save_batch(
        run_id,
        1,
        [
            _outcomes(1),
            _outcomes(2).model_copy(
                update={"should_cost": RowFailure(row_id=2, stage="extract_spec", error="bad")}
            ),
        ],
    )
    store.start_batch(run_id, 2)
    store.fail_batch(run_id, 2, "boom")
    store.start_batch(run_id, 3)
    store.save_batch(run_id, 3, [_outcomes(5)])

    assert store.rows_to_rerun(run_id) == {1: [2], 2: [3, 4]}


# --- run lease (spec 13, phase-8 Q6) ------------------------------------------------------


def test_only_one_owner_can_hold_a_run_lease_until_it_is_released(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run_id = store.create_run(_spec(), [line(1)])

    store.acquire_lease(run_id, "cli", ttl_s=60, now=1000.0)
    with pytest.raises(LeaseHeld) as held:
        store.acquire_lease(run_id, "api", ttl_s=60, now=1030.0)
    assert (held.value.owner, held.value.age_s) == ("cli", 30.0)
    assert _fresh(store, run_id, 60, 1030.0)

    store.release_lease(run_id, "cli")
    assert not _fresh(store, run_id, 60, 1030.0)
    store.acquire_lease(run_id, "api", ttl_s=60, now=1031.0)


def test_an_expired_lease_can_be_taken_over_and_the_old_owner_cannot_renew(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    run_id = store.create_run(_spec(), [line(1)])
    store.acquire_lease(run_id, "worker", ttl_s=60, now=1000.0)

    assert store.renew_lease(run_id, "worker", now=1050.0)
    with pytest.raises(LeaseHeld):
        store.acquire_lease(run_id, "cli", ttl_s=60, now=1100.0)  # renewed at 1050
    store.acquire_lease(run_id, "cli", ttl_s=60, now=1111.0)

    assert not store.renew_lease(run_id, "worker", now=1112.0)
    store.release_lease(run_id, "worker")  # not the owner any more: no effect
    assert _fresh(store, run_id, 60, 1112.0)


def test_the_result_store_uses_wal_and_waits_for_locks(tmp_path: Path) -> None:
    conn = connect(tmp_path / "rfq.db")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 10_000
    finally:
        conn.close()


def test_many_threads_can_open_a_new_database_at_once(tmp_path: Path) -> None:
    # Switching a file to WAL takes an exclusive lock without waiting for the busy timeout;
    # first opens racing each other (tracking writes from executor threads) must not fail.
    errors: list[sqlite3.OperationalError] = []

    def open_and_write(db: Path, start: threading.Barrier) -> None:
        start.wait()
        try:
            conn = connect(db)
            with conn:
                conn.execute("INSERT INTO llm_calls (ts, status) VALUES ('t', 'ok')")
            conn.close()
        except sqlite3.OperationalError as exc:
            errors.append(exc)

    for attempt in range(80):
        db = tmp_path / f"fresh{attempt}.db"
        start = threading.Barrier(16)
        threads = [threading.Thread(target=open_and_write, args=(db, start)) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert errors == []


@pytest.mark.parametrize("version", [(3, 51, 0), (3, 51, 1)])
def test_a_sqlite_known_to_deadlock_is_warned_about(version: tuple[int, int, int]) -> None:
    with pytest.warns(RuntimeWarning, match="3.51.2"):
        check_sqlite_version(version)


@pytest.mark.parametrize("version", [(3, 50, 4), (3, 51, 2), (3, 53, 4)])
def test_other_sqlite_versions_pass_quietly(
    version: tuple[int, int, int], recwarn: pytest.WarningsRecorder
) -> None:
    check_sqlite_version(version)

    assert not [w for w in recwarn if issubclass(w.category, RuntimeWarning)]


def _fresh(store: RunStore, run_id: str, ttl_s: float, now: float | None = None) -> bool:
    lease = store.lease(run_id)
    return lease is not None and lease.fresh(ttl_s, now)
