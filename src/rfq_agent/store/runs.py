"""Result store of orchestrated runs (spec 13): runs, batches, stage outcomes, rows, reviews."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections import Counter
from collections.abc import Collection, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from rfq_agent.orchestrator.prices import price_key
from rfq_agent.schemas import (
    OPEN_BATCH_STATUSES,
    AgentUsage,
    Band,
    BatchRecord,
    InjectionKind,
    LLMUsage,
    MSRPResult,
    OEResolution,
    ResolvedRow,
    RetailResult,
    ReviewEntry,
    ReviewFilter,
    RFQLine,
    RowFailure,
    RowPage,
    RunEval,
    RunLease,
    RunProgress,
    RunRecord,
    RunSpec,
    RunStatus,
    ShouldCostResult,
    StrategyName,
)
from rfq_agent.store.db import BUSY_TIMEOUT_MS, connect

RUNS_DDL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    spec        TEXT NOT NULL,
    status      TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    finished_at TEXT,
    lease_owner TEXT,
    lease_heartbeat REAL
);
CREATE TABLE IF NOT EXISTS rfq_lines (
    run_id  TEXT    NOT NULL,
    row_id  INTEGER NOT NULL,
    data    TEXT    NOT NULL,
    PRIMARY KEY (run_id, row_id)
);
CREATE TABLE IF NOT EXISTS batch_results (
    run_id      TEXT    NOT NULL,
    batch_id    INTEGER NOT NULL,
    row_ids     TEXT    NOT NULL,
    rerun_rows  TEXT,
    status      TEXT    NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    started_at  TEXT,
    finished_at TEXT,
    PRIMARY KEY (run_id, batch_id)
);
CREATE TABLE IF NOT EXISTS row_outcomes (
    run_id      TEXT    NOT NULL,
    row_id      INTEGER NOT NULL,
    batch_id    INTEGER NOT NULL,
    price_key   TEXT,
    msrp_ok     INTEGER NOT NULL,
    retail_ok   INTEGER NOT NULL,
    any_failure INTEGER NOT NULL,
    manual_oe   INTEGER NOT NULL DEFAULT 0,
    msrp_from   INTEGER,
    retail_from INTEGER,
    data        TEXT    NOT NULL,
    PRIMARY KEY (run_id, row_id)
);
CREATE INDEX IF NOT EXISTS idx_row_outcomes_key ON row_outcomes (run_id, price_key);
CREATE TABLE IF NOT EXISTS run_rows (
    run_id        TEXT    NOT NULL,
    row_id        INTEGER NOT NULL,
    score         INTEGER,
    band          TEXT,
    review_status TEXT,
    basis         TEXT    NOT NULL,
    extra_reasons TEXT    NOT NULL,
    data          TEXT    NOT NULL,
    PRIMARY KEY (run_id, row_id)
);
CREATE TABLE IF NOT EXISTS review_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT    NOT NULL,
    row_id       INTEGER NOT NULL,
    ts           TEXT    NOT NULL,
    action       TEXT    NOT NULL,
    oe_before    TEXT,
    oe_after     TEXT,
    msrp_before  REAL,
    msrp_after   REAL,
    retail_before REAL,
    retail_after REAL,
    note         TEXT,
    snapshot     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_log_run ON review_log (run_id, row_id);
CREATE TABLE IF NOT EXISTS run_injections (
    run_id TEXT    NOT NULL,
    row_id INTEGER NOT NULL,
    kind   TEXT    NOT NULL,
    PRIMARY KEY (run_id, row_id)
);
CREATE TABLE IF NOT EXISTS run_evals (
    run_id     TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    data       TEXT NOT NULL
);
"""


class LeaseHeld(RuntimeError):
    """Another process drives the run (spec 13 run lease)."""

    def __init__(self, run_id: str, owner: str, age_s: float) -> None:
        super().__init__(
            f"run {run_id} is driven by {owner} (last heartbeat {age_s:.0f}s ago); "
            "wait for it to finish, or retry once its lease expires"
        )
        self.run_id = run_id
        self.owner = owner
        self.age_s = age_s


class RescoreRefused(ValueError):
    """The source run cannot be re-scored (not finalized, or reviewed by a person)."""


class RowOutcomes(BaseModel):
    """The four agent outcomes of one row, as a batch stores them."""

    row_id: int
    oe: OEResolution | RowFailure
    msrp: MSRPResult | RowFailure
    retail: RetailResult | RowFailure
    should_cost: ShouldCostResult | RowFailure
    manual_oe: bool = False  # OE number entered in review; rerun-failed keeps it
    # Row whose agent run produced this price result (many-to-one copy); None: own run.
    msrp_from: int | None = None
    retail_from: int | None = None

    def has_failure(self) -> bool:
        return any(
            isinstance(o, RowFailure) for o in (self.oe, self.msrp, self.retail, self.should_cost)
        )


class CachedPrices(BaseModel):
    """Reusable price results of earlier completed batches, keyed by normalized current OE."""

    msrp: dict[str, MSRPResult]
    retail: dict[str, RetailResult]


class StoredRow(BaseModel):
    """A finalized row plus what lets a later finalize skip rescoring it."""

    row: ResolvedRow
    basis: str  # fingerprint of the resolved row before scoring and review overlays
    extra_reasons: list[str] = []  # review reasons added by confidence scoring


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_run_id() -> str:
    return f"run-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"


class RunStore:
    """Access layer over the business DB tables of orchestrated runs."""

    def __init__(self, conn: sqlite3.Connection, path: Path) -> None:
        self._conn = conn
        self.path = path
        conn.executescript(RUNS_DDL)
        _add_missing_columns(conn)

    @classmethod
    def open(cls, db_path: Path, *, busy_timeout_ms: int = BUSY_TIMEOUT_MS) -> RunStore:
        return cls(connect(db_path, busy_timeout_ms=busy_timeout_ms), db_path)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._conn:
            yield self._conn

    def _select(
        self, select_from: str, run_id: str, row_ids: Collection[int] | None
    ) -> list[sqlite3.Row]:
        """``SELECT ... FROM table`` of one run by row_id, optionally only some rows."""
        sql = f"{select_from} WHERE run_id = ?"
        params: list[object] = [run_id]
        if row_ids is not None:
            ids = sorted(set(row_ids))
            sql += f" AND row_id IN ({','.join('?' * len(ids))})"
            params += ids
        return self._conn.execute(sql + " ORDER BY row_id", params).fetchall()

    # --- runs ---------------------------------------------------------------------------

    def create_run(self, spec: RunSpec, lines: Sequence[RFQLine]) -> str:
        run_id = new_run_id()
        now = _now()
        ordered = sorted(lines, key=lambda x: x.row_id)
        with self._tx() as tx:
            tx.execute(
                "INSERT INTO runs (run_id, created_at, spec, status, updated_at) "
                "VALUES (?, ?, ?, 'running', ?)",
                (run_id, now, spec.model_dump_json(), now),
            )
            tx.executemany(
                "INSERT INTO rfq_lines VALUES (?, ?, ?)",
                [(run_id, x.row_id, x.model_dump_json()) for x in ordered],
            )
            for index in range(0, len(ordered), spec.batch_size):
                rows = [x.row_id for x in ordered[index : index + spec.batch_size]]
                tx.execute(
                    "INSERT INTO batch_results (run_id, batch_id, row_ids, status) "
                    "VALUES (?, ?, ?, 'pending')",
                    (run_id, index // spec.batch_size + 1, _ids(rows)),
                )
        return run_id

    def run(self, run_id: str) -> RunRecord:
        row = self._conn.execute(
            "SELECT run_id, created_at, spec, status, finished_at FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown run {run_id!r}")
        return _run_record(row)

    def list_runs(self) -> list[RunRecord]:
        """Every run, newest first."""
        rows = self._conn.execute(
            "SELECT run_id, created_at, spec, status, finished_at FROM runs "
            "ORDER BY created_at DESC, rowid DESC"
        ).fetchall()
        return [_run_record(r) for r in rows]

    def set_run_status(self, run_id: str, status: RunStatus) -> None:
        with self._tx() as tx:
            tx.execute(
                "UPDATE runs SET status = ?, updated_at = ? WHERE run_id = ?",
                (status, _now(), run_id),
            )

    def mark_finalized(self, run_id: str, status: RunStatus) -> None:
        """Finalize done: status set; ``finished_at`` stamped when the run was running.

        Later re-summaries during review keep the time the run was first finalized.
        """
        now = _now()
        with self._tx() as tx:
            tx.execute(
                "UPDATE runs SET finished_at = CASE WHEN status = 'running' THEN ? "
                "ELSE finished_at END, status = ?, updated_at = ? WHERE run_id = ?",
                (now, status, now, run_id),
            )

    def check_rescorable(self, run_id: str) -> None:
        """Refuse a run that cannot be re-scored: not finalized, or reviewed by a person.

        Raises ``KeyError`` for an unknown run and ``RescoreRefused`` otherwise.
        """
        if self.run(run_id).status == "running":
            raise RescoreRefused(f"run {run_id} is not finalized yet; re-score a finalized run")
        if self.reviews(run_id):
            raise RescoreRefused(f"run {run_id} has human reviews; re-score a run without review")

    def copy_for_rescore(self, source_id: str, strategy: StrategyName) -> str:
        """A new run holding the source's lines, batches and stage outcomes (spec 13 rescore).

        The copy is ``running`` with every batch as the source left it, so resuming it only
        finalizes (resolution and confidence) with ``strategy``; no stage agent runs again.
        """
        self.check_rescorable(source_id)
        with self._tx() as tx:
            return self._copy_run(tx, source_id, strategy)

    def copy_for_injection(
        self,
        source_id: str,
        outcomes: Sequence[RowOutcomes],
        injections: dict[int, InjectionKind],
    ) -> str:
        """A rescore copy (``evidence``) with ``outcomes`` replaced and the injections recorded.

        One transaction (spec 14 injection experiment): a crash leaves either nothing or the
        whole injected copy, never a copy that silently lost its injected errors.
        """
        self.check_rescorable(source_id)
        rows = {
            r[0]
            for r in self._conn.execute(
                "SELECT row_id FROM rfq_lines WHERE run_id = ?", (source_id,)
            )
        }
        unknown = sorted({*injections, *(o.row_id for o in outcomes)} - rows)
        if unknown:
            raise KeyError(f"rows {unknown} are not in run {source_id}")
        with self._tx() as tx:
            run_id = self._copy_run(tx, source_id, "evidence")
            self._write_outcomes(tx, run_id, outcomes, None)
            tx.executemany(
                "INSERT OR REPLACE INTO run_injections (run_id, row_id, kind) VALUES (?, ?, ?)",
                [(run_id, row_id, kind) for row_id, kind in injections.items()],
            )
        return run_id

    def _copy_run(self, tx: sqlite3.Connection, source_id: str, strategy: StrategyName) -> str:
        """A new ``running`` run (its ``runs`` row) with the source's lines, batches, outcomes
        and injections, written inside ``tx``; returns the new run_id."""
        source = self.run(source_id)
        spec = source.spec.model_copy(
            update={"strategy": strategy, "review": False, "rescored_from": source_id}
        )
        run_id = new_run_id()
        now = _now()
        tx.execute(
            "INSERT INTO runs (run_id, created_at, spec, status, updated_at) "
            "VALUES (?, ?, ?, 'running', ?)",
            (run_id, now, spec.model_dump_json(), now),
        )
        tx.execute(
            "INSERT INTO rfq_lines (run_id, row_id, data) "
            "SELECT ?, row_id, data FROM rfq_lines WHERE run_id = ?",
            (run_id, source_id),
        )
        tx.execute(
            "INSERT INTO batch_results (run_id, batch_id, row_ids, status, error) "
            "SELECT ?, batch_id, row_ids, status, error FROM batch_results WHERE run_id = ?",
            (run_id, source_id),
        )
        tx.execute(
            # Name the columns: a migrated database has them in another order.
            f"INSERT INTO row_outcomes (run_id, {_OUTCOME_COLUMNS}) "
            f"SELECT ?, {_OUTCOME_COLUMNS} FROM row_outcomes WHERE run_id = ?",
            (run_id, source_id),
        )
        tx.execute(
            "INSERT INTO run_injections (run_id, row_id, kind) "
            "SELECT ?, row_id, kind FROM run_injections WHERE run_id = ?",
            (run_id, source_id),
        )
        return run_id

    # --- error injection (spec 14 experiment) -------------------------------------------

    def injections(self, run_id: str) -> dict[int, InjectionKind]:
        """Injected rows of the run by row_id (empty for an ordinary run)."""
        rows = self._conn.execute(
            "SELECT row_id, kind FROM run_injections WHERE run_id = ? ORDER BY row_id", (run_id,)
        ).fetchall()
        return {r["row_id"]: r["kind"] for r in rows}

    # --- run lease ----------------------------------------------------------------------

    def acquire_lease(
        self, run_id: str, owner: str, *, ttl_s: float, now: float | None = None
    ) -> None:
        """Take the run for ``owner``: free, already ours, or its heartbeat expired.

        Raises ``LeaseHeld`` when another live process drives the run (spec 13).
        """
        at = time.time() if now is None else now
        with self._tx() as tx:
            taken = tx.execute(
                "UPDATE runs SET lease_owner = ?, lease_heartbeat = ? WHERE run_id = ? AND "
                "(lease_owner IS NULL OR lease_owner = ? OR lease_heartbeat < ?)",
                (owner, at, run_id, owner, at - ttl_s),
            ).rowcount
        if not taken:
            holder = self.lease(run_id)
            if holder is None:  # the run does not exist (a free lease is always taken)
                raise KeyError(f"unknown run {run_id!r}")
            raise LeaseHeld(run_id, holder.owner, at - holder.heartbeat)

    def take_over_lease(self, run_id: str, old: str, new: str) -> bool:
        """Replace ``old`` by ``new`` only if ``old`` still holds the lease."""
        with self._tx() as tx:
            return bool(
                tx.execute(
                    "UPDATE runs SET lease_owner = ?, lease_heartbeat = ? "
                    "WHERE run_id = ? AND lease_owner = ?",
                    (new, time.time(), run_id, old),
                ).rowcount
            )

    def renew_lease(self, run_id: str, owner: str, *, now: float | None = None) -> bool:
        """Refresh the heartbeat; False when ``owner`` no longer holds the lease."""
        at = time.time() if now is None else now
        with self._tx() as tx:
            return bool(
                tx.execute(
                    "UPDATE runs SET lease_heartbeat = ? WHERE run_id = ? AND lease_owner = ?",
                    (at, run_id, owner),
                ).rowcount
            )

    def release_lease(self, run_id: str, owner: str) -> None:
        with self._tx() as tx:
            tx.execute(
                "UPDATE runs SET lease_owner = NULL, lease_heartbeat = NULL "
                "WHERE run_id = ? AND lease_owner = ?",
                (run_id, owner),
            )

    def lease(self, run_id: str) -> RunLease | None:
        """The run's lease, or None when nobody holds it."""
        row = self._conn.execute(
            "SELECT lease_owner, lease_heartbeat FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None or row["lease_owner"] is None:
            return None
        return RunLease(owner=row["lease_owner"], heartbeat=row["lease_heartbeat"])

    def lines(self, run_id: str, row_ids: Collection[int] | None = None) -> list[RFQLine]:
        rows = self._select("SELECT data FROM rfq_lines", run_id, row_ids)
        return [RFQLine.model_validate_json(r["data"]) for r in rows]

    # --- batches ------------------------------------------------------------------------

    def batches(self, run_id: str) -> list[BatchRecord]:
        rows = self._conn.execute(
            "SELECT batch_id, row_ids, rerun_rows, status, attempts, error FROM batch_results "
            "WHERE run_id = ? ORDER BY batch_id",
            (run_id,),
        ).fetchall()
        return [
            BatchRecord(
                batch_id=r["batch_id"],
                row_ids=_parse_ids(r["row_ids"]),
                rerun_rows=_parse_ids(r["rerun_rows"]) if r["rerun_rows"] else None,
                status=r["status"],
                attempts=r["attempts"],
                error=r["error"],
            )
            for r in rows
        ]

    def progress(self, run_id: str) -> RunProgress:
        counts = Counter(b.status for b in self.batches(run_id))
        return RunProgress(
            batches_total=sum(counts.values()),
            pending=counts["pending"],
            running=counts["running"],
            completed=counts["completed"],
            failed=counts["failed"],
        )

    def start_batch(self, run_id: str, batch_id: int) -> None:
        """Mark a batch running and count the execution (a resumed batch counts again)."""
        with self._tx() as tx:
            tx.execute(
                "UPDATE batch_results SET status = 'running', attempts = attempts + 1, "
                "started_at = ? WHERE run_id = ? AND batch_id = ?",
                (_now(), run_id, batch_id),
            )

    def save_batch(self, run_id: str, batch_id: int, outcomes: Sequence[RowOutcomes]) -> None:
        """Write the batch's outcomes and mark it completed, in one transaction."""
        with self._tx() as tx:
            self._write_outcomes(tx, run_id, outcomes, batch_id)
            tx.execute(
                "UPDATE batch_results SET status = 'completed', error = NULL, rerun_rows = NULL, "
                "finished_at = ? WHERE run_id = ? AND batch_id = ?",
                (_now(), run_id, batch_id),
            )

    def reopen_for_rerun(self, run_id: str, plan: dict[int, list[int]]) -> None:
        """Record a rerun plan (spec 13) so that a resume after a crash finishes all of it.

        In one transaction: the run is open again and every planned batch is pending with
        the rows it reruns.
        """
        with self._tx() as tx:
            tx.execute(
                "UPDATE runs SET status = 'running', updated_at = ? WHERE run_id = ?",
                (_now(), run_id),
            )
            tx.executemany(
                "UPDATE batch_results SET status = 'pending', rerun_rows = ? "
                "WHERE run_id = ? AND batch_id = ?",
                [(_ids(rows), run_id, batch_id) for batch_id, rows in plan.items()],
            )

    def fail_batch(self, run_id: str, batch_id: int, error: str) -> None:
        with self._tx() as tx:
            tx.execute(
                "UPDATE batch_results SET status = 'failed', error = ?, rerun_rows = NULL, "
                "finished_at = ? WHERE run_id = ? AND batch_id = ?",
                (error, _now(), run_id, batch_id),
            )

    # --- stage outcomes -----------------------------------------------------------------

    def save_outcomes(self, run_id: str, outcomes: Sequence[RowOutcomes]) -> None:
        """Replace stored outcomes of some rows (rerun-failed, OE changed in review)."""
        with self._tx() as tx:
            self._write_outcomes(tx, run_id, outcomes, None)

    def outcomes(self, run_id: str, row_ids: Collection[int] | None = None) -> list[RowOutcomes]:
        rows = self._select("SELECT data FROM row_outcomes", run_id, row_ids)
        return [RowOutcomes.model_validate_json(r["data"]) for r in rows]

    def cached_prices(self, run_id: str, keys: Collection[str]) -> CachedPrices:
        """Reusable price results by key; the smallest row_id per key wins (spec 13).

        Only results a row's own agent run produced (not many-to-one copies), and not the
        rows an open batch is about to run: a rerun row runs all four paths afresh.
        """
        cached = CachedPrices(msrp={}, retail={})
        if not keys:
            return cached
        marks = ",".join("?" * len(keys))
        rows = self._conn.execute(
            "SELECT row_id, price_key, msrp_ok, retail_ok, msrp_from, retail_from, data "
            f"FROM row_outcomes WHERE run_id = ? AND price_key IN ({marks}) ORDER BY row_id",
            (run_id, *keys),
        ).fetchall()
        running = self._rows_in_open_batches(run_id)
        for r in rows:
            if r["row_id"] in running:
                continue
            stored = RowOutcomes.model_validate_json(r["data"])
            if _own_success(r, "msrp") and isinstance(stored.msrp, MSRPResult):
                cached.msrp.setdefault(r["price_key"], stored.msrp)
            if _own_success(r, "retail") and isinstance(stored.retail, RetailResult):
                cached.retail.setdefault(r["price_key"], stored.retail)
        return cached

    def _rows_in_open_batches(self, run_id: str) -> set[int]:
        """Rows the open batches will run: a pending rerun's rows, else the whole batch."""
        return {
            row
            for b in self.batches(run_id)
            if b.status in OPEN_BATCH_STATUSES
            for row in b.rows_to_run()
        }

    def rows_to_rerun(self, run_id: str) -> dict[int, list[int]]:
        """Batch -> rows to rerun: every row of a failed batch, plus rows with a failure."""
        failing = {
            r["row_id"]
            for r in self._conn.execute(
                "SELECT row_id FROM row_outcomes WHERE run_id = ? AND any_failure = 1", (run_id,)
            )
        }
        plan: dict[int, list[int]] = {}
        for batch in self.batches(run_id):
            if batch.status == "failed":
                rows = batch.row_ids
            elif batch.status == "completed":
                rows = [r for r in batch.row_ids if r in failing]
            else:
                rows = []
            if rows:
                plan[batch.batch_id] = rows
        return plan

    def _write_outcomes(
        self,
        tx: sqlite3.Connection,
        run_id: str,
        outcomes: Sequence[RowOutcomes],
        batch_id: int | None,
    ) -> None:
        for item in outcomes:
            batch = batch_id if batch_id is not None else self._batch_of(run_id, item.row_id)
            tx.execute(
                "INSERT OR REPLACE INTO row_outcomes (run_id, row_id, batch_id, price_key, "
                "msrp_ok, retail_ok, any_failure, manual_oe, data, msrp_from, retail_from) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    item.row_id,
                    batch,
                    price_key(item.oe),
                    not isinstance(item.msrp, RowFailure),
                    not isinstance(item.retail, RowFailure),
                    item.has_failure(),
                    item.manual_oe,
                    item.model_dump_json(),
                    item.msrp_from,
                    item.retail_from,
                ),
            )

    def _batch_of(self, run_id: str, row_id: int) -> int:
        for batch in self.batches(run_id):
            if row_id in batch.row_ids:
                return batch.batch_id
        raise KeyError(f"row {row_id} is not in run {run_id!r}")

    # --- finalized rows -----------------------------------------------------------------

    def save_rows(self, run_id: str, rows: Sequence[StoredRow]) -> None:
        with self._tx() as tx:
            tx.executemany(
                "INSERT OR REPLACE INTO run_rows VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        run_id,
                        item.row.line.row_id,
                        item.row.worksheet.confidence_score,
                        item.row.worksheet.band,
                        item.row.worksheet.review_status,
                        item.basis,
                        json.dumps(item.extra_reasons, ensure_ascii=False),
                        item.row.model_dump_json(),
                    )
                    for item in rows
                ],
            )

    def stored_rows(self, run_id: str, row_ids: Collection[int] | None = None) -> list[StoredRow]:
        rows = self._select("SELECT basis, extra_reasons, data FROM run_rows", run_id, row_ids)
        return [
            StoredRow(
                row=ResolvedRow.model_validate_json(r["data"]),
                basis=r["basis"],
                extra_reasons=json.loads(r["extra_reasons"]),
            )
            for r in rows
        ]

    def rows(self, run_id: str, row_ids: Collection[int] | None = None) -> list[ResolvedRow]:
        return [item.row for item in self.stored_rows(run_id, row_ids)]

    def row_page(
        self,
        run_id: str,
        *,
        bands: Sequence[Band],
        statuses: Sequence[ReviewFilter],
        offset: int,
        limit: int,
    ) -> RowPage:
        """Worksheet rows by band and review status (any of each; empty means all)."""
        where = "WHERE run_id = ?"
        params: list[object] = [run_id]
        if bands:
            where += f" AND band IN ({','.join('?' * len(bands))})"
            params += bands
        if statuses:
            reviewed = [s for s in statuses if s != "unreviewed"]
            parts = ["review_status IS NULL"] if "unreviewed" in statuses else []
            if reviewed:
                parts.append(f"review_status IN ({','.join('?' * len(reviewed))})")
                params += reviewed
            where += f" AND ({' OR '.join(parts)})"
        total = self._conn.execute(f"SELECT COUNT(*) FROM run_rows {where}", params).fetchone()[0]
        found = self._conn.execute(
            f"SELECT data FROM run_rows {where} ORDER BY row_id LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        items = [ResolvedRow.model_validate_json(r["data"]).worksheet for r in found]
        return RowPage(total=total, offset=offset, limit=limit, items=items)

    def llm_usage(self, run_id: str) -> LLMUsage:
        """LLM calls of the run; cost is None when a successful call has no price."""
        agents = self.agent_usage(run_id)
        costs = [a.cost_usd for a in agents]
        return LLMUsage(
            calls=sum(a.calls for a in agents),
            input_tokens=sum(a.input_tokens for a in agents),
            output_tokens=sum(a.output_tokens for a in agents),
            cost_usd=None if None in costs else sum(c for c in costs if c is not None),
        )

    def agent_usage(self, run_id: str) -> list[AgentUsage]:
        """LLM calls of the run grouped by agent; cost None when a call there is unpriced."""
        rows = self._conn.execute(
            "SELECT agent, COUNT(*), SUM(status = 'error'), COALESCE(SUM(input_tokens), 0), "
            "COALESCE(SUM(output_tokens), 0), SUM(cost_usd), "
            "SUM(status = 'ok' AND cost_usd IS NULL) "
            "FROM llm_calls WHERE rfq_run_id = ? GROUP BY agent ORDER BY MIN(id)",
            (run_id,),
        ).fetchall()
        return [
            AgentUsage(
                agent=agent or "-",
                calls=calls,
                errors=errors or 0,
                input_tokens=tokens_in,
                output_tokens=tokens_out,
                cost_usd=None if unpriced else (cost or 0.0),
            )
            for agent, calls, errors, tokens_in, tokens_out, cost, unpriced in rows
        ]

    def llm_models(self, run_id: str) -> dict[str, str]:
        """Role -> ``provider/model`` the run's calls used (several joined by commas)."""
        rows = self._conn.execute(
            "SELECT DISTINCT role, provider, model FROM llm_calls WHERE rfq_run_id = ? "
            "ORDER BY role, provider, model",
            (run_id,),
        ).fetchall()
        models: dict[str, list[str]] = {}
        for role, provider, model in rows:
            models.setdefault(role or "-", []).append(f"{provider}/{model}")
        return {role: ", ".join(names) for role, names in models.items()}

    # --- evaluations --------------------------------------------------------------------

    def save_evaluation(self, evaluation: RunEval) -> None:
        """Store a run's evaluation; evaluating the run again replaces it."""
        with self._tx() as tx:
            tx.execute(
                "INSERT OR REPLACE INTO run_evals VALUES (?, ?, ?)",
                (evaluation.run_id, evaluation.created_at, evaluation.model_dump_json()),
            )

    def evaluation(self, run_id: str) -> RunEval | None:
        row = self._conn.execute(
            "SELECT data FROM run_evals WHERE run_id = ?", (run_id,)
        ).fetchone()
        return None if row is None else RunEval.model_validate_json(row["data"])

    # --- review log ---------------------------------------------------------------------

    def log_review(self, run_id: str, entry: ReviewEntry) -> None:
        with self._tx() as tx:
            tx.execute(
                "INSERT INTO review_log (run_id, row_id, ts, action, oe_before, oe_after, "
                "msrp_before, msrp_after, retail_before, retail_after, note, snapshot) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    entry.row_id,
                    entry.ts or _now(),
                    entry.action,
                    entry.oe_before,
                    entry.oe_after,
                    entry.msrp_before,
                    entry.msrp_after,
                    entry.retail_before,
                    entry.retail_after,
                    entry.note,
                    entry.snapshot.model_dump_json(),
                ),
            )

    def reviews(self, run_id: str, row_id: int | None = None) -> list[ReviewEntry]:
        sql = "SELECT * FROM review_log WHERE run_id = ?"
        params: list[object] = [run_id]
        if row_id is not None:
            sql += " AND row_id = ?"
            params.append(row_id)
        rows = self._conn.execute(sql + " ORDER BY id", params).fetchall()
        return [
            ReviewEntry(
                row_id=r["row_id"],
                action=r["action"],
                oe_before=r["oe_before"],
                oe_after=r["oe_after"],
                msrp_before=r["msrp_before"],
                msrp_after=r["msrp_after"],
                retail_before=r["retail_before"],
                retail_after=r["retail_after"],
                note=r["note"],
                snapshot=ResolvedRow.model_validate_json(r["snapshot"]),
                ts=r["ts"],
            )
            for r in rows
        ]


_OUTCOME_COLUMNS = (
    "row_id, batch_id, price_key, msrp_ok, retail_ok, any_failure, manual_oe, "
    "msrp_from, retail_from, data"
)


def _run_record(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        run_id=row["run_id"],
        created_at=row["created_at"],
        spec=RunSpec.model_validate_json(row["spec"]),
        status=row["status"],
        finished_at=row["finished_at"],
    )


def _own_success(row: sqlite3.Row, path: Literal["msrp", "retail"]) -> bool:
    """The path is not a row failure and the row's own agent run produced it."""
    return bool(row[f"{path}_ok"]) and row[f"{path}_from"] is None


_ADDED_COLUMNS = {
    "runs": {"finished_at": "TEXT", "lease_owner": "TEXT", "lease_heartbeat": "REAL"},
    "batch_results": {"rerun_rows": "TEXT"},
    "row_outcomes": {"msrp_from": "INTEGER", "retail_from": "INTEGER"},
}


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """Columns added after a database was created (CREATE IF NOT EXISTS skips them)."""
    for table, columns in _ADDED_COLUMNS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, kind in columns.items():
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
    conn.commit()


@contextmanager
def open_store(db_path: Path) -> Iterator[RunStore]:
    """A ``RunStore`` that is closed when the block ends, also on errors."""
    store = RunStore.open(db_path)
    try:
        yield store
    finally:
        store.close()


def _ids(row_ids: Sequence[int]) -> str:
    return ",".join(str(r) for r in row_ids)


def _parse_ids(text: str) -> list[int]:
    return [int(r) for r in text.split(",") if r]
