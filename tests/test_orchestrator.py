"""Orchestrated runs (spec 13): batches, price reuse, resume, rerun-failed, review."""

from __future__ import annotations

import asyncio
import json
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from rfq_agent.agents.runner import (
    run_oe_interchange,
    run_oe_msrp,
    run_retail_price,
    run_should_cost,
)
from rfq_agent.agents.should_cost import ShouldCostInput
from rfq_agent.confidence import fake_handlers as confidence_fakes
from rfq_agent.confidence import score_rows
from rfq_agent.config import Settings
from rfq_agent.intake import read_rfq
from rfq_agent.llm.fake import FakeRequest, register_fake_handler
from rfq_agent.mcp_server import server as internal
from rfq_agent.orchestrator.lease import LeaseLost, hold_lease
from rfq_agent.orchestrator.run import (
    AgentDeps,
    NotInReviewQueue,
    ReviewNotOpen,
    RunOptions,
    answer_review,
    open_checkpointer,
    rerun_failed,
    resume_run,
    review_queue,
    start_run,
)
from rfq_agent.partno import normalize_part_no
from rfq_agent.resolution import resolve_rows
from rfq_agent.schemas import (
    MaterialPrice,
    OEResolution,
    PartSpec,
    ResolvedRow,
    ReviewDecision,
    ReviewInput,
    ReviewPrompt,
    RFQLine,
    RunSpec,
)
from rfq_agent.store.runs import LeaseHeld, RunStore
from rfq_agent.tools import build_tools
from rfq_agent.tools.base import ToolHit, ToolProvider, ToolTimeoutError


class DirectInternalDB:
    """The MCP server's tool functions without the stdio hop."""

    async def get_part_spec(self, our_part_no: str) -> PartSpec:
        return internal.get_part_spec(our_part_no)

    async def list_materials(self) -> list[str]:
        return internal.list_materials().materials

    async def get_material_price(self, material: str) -> MaterialPrice:
        return internal.get_material_price(material)


async def _lookup(customer_part_no: str) -> str | None:
    found = internal.lookup_our_part(customer_part_no)
    return found.our_part_no if found.found else None


@pytest.fixture
def deps(settings: Settings, generated_dataset: Path, monkeypatch: pytest.MonkeyPatch) -> AgentDeps:
    monkeypatch.setenv(internal.ENV_INTERNAL_DB, str(generated_dataset / "internal.db"))
    return AgentDeps(
        lookup=_lookup,
        internal_db=DirectInternalDB(),
        tools=build_tools(generated_dataset, settings.tools),
    )


@pytest.fixture
def lines(generated_dataset: Path) -> list[RFQLine]:
    return read_rfq(generated_dataset / "rfq_200.xlsx").lines[:60]


def _spec(generated_dataset: Path, batch_size: int = 25, **kw: object) -> RunSpec:
    fields: dict[str, object] = {
        "input": "rfq_200.xlsx",
        "dataset_dir": str(generated_dataset),
        "batch_size": batch_size,
        "strategy": "evidence",
        "review": False,
    }
    fields.update(kw)
    return RunSpec.model_validate(fields)


def _rows(settings: Settings, run_id: str) -> list[ResolvedRow]:
    store = RunStore.open(settings.db_path)
    try:
        return store.rows(run_id)
    finally:
        store.close()


async def _one_shot(
    settings: Settings, deps: AgentDeps, lines: list[RFQLine], dataset: Path
) -> list[ResolvedRow]:
    """The P6 path: every agent over the whole table, then resolution and confidence."""
    oe = await run_oe_interchange(
        lines, settings, dataset_dir=dataset, run_id="ref", lookup=deps.lookup
    )
    msrp = await run_oe_msrp(oe, settings, dataset_dir=dataset, run_id="ref")
    retail = await run_retail_price(oe, settings, dataset_dir=dataset, run_id="ref")
    cost = await run_should_cost(
        [ShouldCostInput(line=x, upstream=o) for x, o in zip(lines, oe, strict=True)],
        settings,
        dataset_dir=dataset,
        run_id="ref",
        internal_db=deps.internal_db,
    )
    return await score_rows(resolve_rows(lines, oe, msrp, retail, cost), settings, "evidence")


async def test_a_batched_run_matches_running_each_stage_over_the_whole_table(
    settings: Settings, deps: AgentDeps, lines: list[RFQLine], generated_dataset: Path
) -> None:
    report = await start_run(settings, _spec(generated_dataset, 25), lines, RunOptions(deps=deps))

    assert report.status == "completed"
    assert [(b.status, b.attempts) for b in report.batches] == [("completed", 1)] * 3
    expected = await _one_shot(settings, deps, lines, generated_dataset)
    assert _rows(settings, report.run_id) == expected


def _started_rows(trace_path: Path, agent: str) -> list[int]:
    events = [json.loads(x) for x in trace_path.read_text(encoding="utf-8").splitlines()]
    return sorted(e["row_id"] for e in events if e["agent"] == agent and e["step"] == "start")


async def test_each_current_oe_is_priced_once_per_run_across_batches(
    settings: Settings, deps: AgentDeps, generated_dataset: Path, tmp_path: Path
) -> None:
    all_lines = read_rfq(generated_dataset / "rfq_200.xlsx").lines
    trace = tmp_path / "run.trace.jsonl"

    report = await start_run(
        settings,
        _spec(generated_dataset, 50),
        all_lines,
        RunOptions(deps=deps, trace_path=trace),
    )

    rows = _rows(settings, report.run_id)
    shared_later = [
        r.line.row_id
        for r in rows
        if r.worksheet.shared_oe_rows and min(r.worksheet.shared_oe_rows) < r.line.row_id
    ]
    assert shared_later, "dataset should contain many-to-one rows"
    once = sorted(set(range(1, 201)) - set(shared_later))
    assert _started_rows(trace, "oe_msrp") == once
    assert _started_rows(trace, "retail_price") == once
    assert _started_rows(trace, "should_cost") == list(range(1, 201))


def _fail_first_save_of(batch_id: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """The business DB rejects one batch's write once (a batch-level failure)."""
    original = RunStore.save_batch
    failed: list[int] = []

    def flaky(self: RunStore, run_id: str, batch: int, outcomes: Any) -> None:
        if batch == batch_id and not failed:
            failed.append(batch)
            raise sqlite3.OperationalError("database is locked")
        original(self, run_id, batch, outcomes)

    monkeypatch.setattr(RunStore, "save_batch", flaky)


async def test_a_failed_batch_does_not_stop_the_run_and_rerun_failed_repairs_it(
    settings: Settings,
    deps: AgentDeps,
    lines: list[RFQLine],
    generated_dataset: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clean = await start_run(settings, _spec(generated_dataset, 25), lines, RunOptions(deps=deps))
    _fail_first_save_of(2, monkeypatch)

    broken = await start_run(settings, _spec(generated_dataset, 25), lines, RunOptions(deps=deps))

    assert [b.status for b in broken.batches] == ["completed", "failed", "completed"]
    assert broken.batches[1].error == "OperationalError: database is locked"
    assert broken.status == "completed"
    rows = _rows(settings, broken.run_id)
    assert len(rows) == 60
    failed_rows = [r for r in rows if r.line.row_id in broken.batches[1].row_ids]
    assert all(r.worksheet.band == "red" for r in failed_rows)
    assert all(f.startswith("oe:batch:") for r in failed_rows for f in r.worksheet.failures)

    repaired = await rerun_failed(settings, broken.run_id, RunOptions(deps=deps))

    assert [(b.status, b.attempts) for b in repaired.batches] == [
        ("completed", 1),
        ("completed", 2),
        ("completed", 1),
    ]
    assert _rows(settings, broken.run_id) == _rows(settings, clean.run_id)


class SimulatedKill(BaseException):
    """Stands in for the process being killed: no row-level handler catches it."""


class CrashingTool:
    """Wraps a tool; raises SimulatedKill on the first search after ``armed`` is set."""

    def __init__(self, inner: ToolProvider) -> None:
        self.name = inner.name
        self._inner = inner
        self.armed = False

    async def search(self, query: str) -> list[ToolHit]:
        if self.armed:
            self.armed = False
            raise SimulatedKill
        return await self._inner.search(query)


async def test_resume_after_a_crash_skips_completed_batches_and_finished_tasks(
    settings: Settings,
    deps: AgentDeps,
    lines: list[RFQLine],
    generated_dataset: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clean = await start_run(settings, _spec(generated_dataset, 25), lines, RunOptions(deps=deps))
    crashing = CrashingTool(deps.tools["retail_search_1"])
    tools = {**deps.tools, "retail_search_1": crashing}
    crash_deps = AgentDeps(lookup=deps.lookup, internal_db=deps.internal_db, tools=tools)
    original = RunStore.save_batch

    def arm_after_first_batch(self: RunStore, run_id: str, batch: int, outcomes: Any) -> None:
        original(self, run_id, batch, outcomes)
        crashing.armed = batch == 1

    monkeypatch.setattr(RunStore, "save_batch", arm_after_first_batch)
    trace = tmp_path / "run.trace.jsonl"
    store = RunStore.open(settings.db_path)
    run_id = store.create_run(_spec(generated_dataset, 25), lines)
    store.close()

    with pytest.raises(SimulatedKill):
        await resume_run(settings, run_id, RunOptions(deps=crash_deps, trace_path=trace))
    report = await resume_run(settings, run_id, RunOptions(deps=crash_deps, trace_path=trace))

    assert [(b.status, b.attempts) for b in report.batches] == [
        ("completed", 1),
        ("completed", 2),
        ("completed", 1),
    ]
    assert _started_rows(trace, "oe_interchange") == [x.row_id for x in lines]
    assert _rows(settings, run_id) == _rows(settings, clean.run_id)


Decide = Callable[[ResolvedRow], dict[str, Any] | None]


class ScriptedReviewer:
    """Answers review prompts; ``by_row`` decides per row, else the next ``script`` entry.

    A decision of ``None`` means the reviewer quits; with nothing scripted it accepts.
    """

    def __init__(self, script: list[Decide] | None = None, by_row: dict[int, Decide] | None = None):
        self.script = list(script or [])
        self.by_row = by_row or {}
        self.seen: list[int] = []

    async def __call__(self, prompt: ReviewPrompt) -> ReviewInput | None:
        rid = prompt.row.line.row_id
        self.seen.append(rid)
        if rid in self.by_row:
            answer = self.by_row[rid](prompt.row)
        else:
            answer = self.script.pop(0)(prompt.row) if self.script else {"action": "accept"}
        return None if answer is None else ReviewInput.model_validate(answer)


async def test_review_walks_red_rows_once_and_applies_each_decision(
    settings: Settings, deps: AgentDeps, lines: list[RFQLine], generated_dataset: Path
) -> None:
    plain = await start_run(settings, _spec(generated_dataset, 25), lines, RunOptions(deps=deps))
    before = {r.line.row_id: r for r in _rows(settings, plain.run_id)}
    red = [rid for rid, r in before.items() if r.worksheet.band == "red"]
    multiple = next(rid for rid in red if before[rid].worksheet.oe_status == "multiple")
    others = [rid for rid in red if rid != multiple][:3]
    plan: dict[int, Decide] = {
        multiple: lambda r: {"action": "modify_oe", "oe": r.worksheet.oe_candidates[0]},
        others[0]: lambda r: {"action": "reject", "note": "not our part"},
        others[1]: lambda r: {"action": "modify_price", "msrp": 42.0, "retail": 40.0},
    }
    reviewer = ScriptedReviewer(by_row=plan)

    report = await start_run(
        settings,
        _spec(generated_dataset, 25, review=True),
        lines,
        RunOptions(deps=deps, reviewer=reviewer),
    )

    assert report.status == "completed"
    assert reviewer.seen == sorted(red)
    rows = {r.line.row_id: r for r in _rows(settings, report.run_id)}
    status = {rid: rows[rid].worksheet.review_status for rid in red}
    assert status[others[0]] == "rejected" and status[others[1]] == "modified"
    assert {s for rid, s in status.items() if rid not in plan} == {"accepted"}
    assert (rows[others[1]].worksheet.msrp, rows[others[1]].worksheet.selected_retail_price) == (
        42.0,
        40.0,
    )
    chosen = rows[multiple]
    assert chosen.worksheet.review_status == "modified"
    assert chosen.worksheet.oe_status == "resolved"
    assert chosen.worksheet.current_oe == before[multiple].worksheet.oe_candidates[0]
    assert chosen.worksheet.msrp_status is not None  # priced after the manual OE
    assert chosen.confidence is not None and chosen.confidence.signals["oe_match"] == 1.0
    unreviewed = [rid for rid in rows if rid not in red]
    assert all(
        rows[rid] == before[rid] for rid in unreviewed if not rows[rid].worksheet.shared_oe_rows
    )
    store = RunStore.open(settings.db_path)
    log = store.reviews(report.run_id)
    store.close()
    assert [e.row_id for e in log] == sorted(red)
    assert [e for e in log if e.row_id == multiple][0].oe_after == chosen.worksheet.current_oe


async def test_quitting_review_pauses_the_run_and_resume_continues_where_it_stopped(
    settings: Settings, deps: AgentDeps, lines: list[RFQLine], generated_dataset: Path
) -> None:
    first = ScriptedReviewer([lambda r: {"action": "accept"}, lambda r: None])

    paused = await start_run(
        settings,
        _spec(generated_dataset, 25, review=True),
        lines,
        RunOptions(deps=deps, reviewer=first),
    )

    assert paused.status == "awaiting_review"
    second = ScriptedReviewer([])
    done = await resume_run(settings, paused.run_id, RunOptions(deps=deps, reviewer=second))
    assert done.status == "completed"
    assert second.seen[0] == first.seen[1]
    assert first.seen[0] not in second.seen
    store = RunStore.open(settings.db_path)
    reviewed = [e.row_id for e in store.reviews(paused.run_id)]
    store.close()
    assert reviewed == [first.seen[0], *second.seen]


class FailingOnceDB(DirectInternalDB):
    """Internal DB whose first spec lookup of each given part errors (should_cost fails)."""

    def __init__(self, *our_part_nos: str) -> None:
        self.targets = set(our_part_nos)

    async def get_part_spec(self, our_part_no: str) -> PartSpec:
        if our_part_no in self.targets:
            self.targets.discard(our_part_no)
            raise RuntimeError("internal DB unavailable")
        return await super().get_part_spec(our_part_no)


async def test_rerun_failed_reruns_only_the_failed_rows_of_a_completed_batch(
    settings: Settings,
    deps: AgentDeps,
    lines: list[RFQLine],
    generated_dataset: Path,
    tmp_path: Path,
) -> None:
    clean = await start_run(settings, _spec(generated_dataset, 25), lines, RunOptions(deps=deps))
    target = next(x for x in lines[:25] if x.our_part_no)
    flaky = AgentDeps(
        lookup=deps.lookup, internal_db=FailingOnceDB(target.our_part_no or ""), tools=deps.tools
    )
    broken = await start_run(settings, _spec(generated_dataset, 25), lines, RunOptions(deps=flaky))
    failed = [r for r in _rows(settings, broken.run_id) if r.worksheet.failures]
    assert [r.line.row_id for r in failed] == [target.row_id]

    trace = tmp_path / "rerun.trace.jsonl"
    repaired = await rerun_failed(settings, broken.run_id, RunOptions(deps=flaky, trace_path=trace))

    for agent in ("oe_interchange", "oe_msrp", "retail_price", "should_cost"):
        assert _started_rows(trace, agent) == [target.row_id], agent
    assert [b.attempts for b in repaired.batches] == [2, 1, 1]
    assert _rows(settings, broken.run_id) == _rows(settings, clean.run_id)


class KillOnArmDB(DirectInternalDB):
    """Internal DB that raises SimulatedKill on the first spec lookup after ``armed``."""

    armed = False

    async def get_part_spec(self, our_part_no: str) -> PartSpec:
        if self.armed:
            self.armed = False
            raise SimulatedKill
        return await super().get_part_spec(our_part_no)


async def test_resume_finishes_a_rerun_that_was_killed(
    settings: Settings, deps: AgentDeps, lines: list[RFQLine], generated_dataset: Path
) -> None:
    clean = await start_run(settings, _spec(generated_dataset, 25), lines, RunOptions(deps=deps))
    target = next(x for x in lines[:25] if x.our_part_no)
    flaky = AgentDeps(
        lookup=deps.lookup, internal_db=FailingOnceDB(target.our_part_no or ""), tools=deps.tools
    )
    broken = await start_run(settings, _spec(generated_dataset, 25), lines, RunOptions(deps=flaky))
    killer = KillOnArmDB()
    killer.armed = True
    killing = AgentDeps(lookup=deps.lookup, internal_db=killer, tools=deps.tools)

    with pytest.raises(SimulatedKill):
        await rerun_failed(settings, broken.run_id, RunOptions(deps=killing))
    report = await resume_run(settings, broken.run_id, RunOptions(deps=deps))

    assert report.status == "completed"
    assert _rows(settings, broken.run_id) == _rows(settings, clean.run_id)


async def test_a_manual_oe_saved_before_a_crash_is_not_priced_again_on_resume(
    settings: Settings,
    deps: AgentDeps,
    lines: list[RFQLine],
    generated_dataset: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plain = await start_run(settings, _spec(generated_dataset, 25), lines, RunOptions(deps=deps))
    multiple = next(r for r in _rows(settings, plain.run_id) if r.worksheet.oe_status == "multiple")
    choose: dict[int, Decide] = {
        multiple.line.row_id: lambda r: {"action": "modify_oe", "oe": r.worksheet.oe_candidates[0]}
    }
    original = RunStore.log_review
    killed: list[int] = []

    def kill_once(self: RunStore, run_id: str, entry: Any) -> None:
        if entry.action == "modify_oe" and not killed:
            killed.append(entry.row_id)
            raise SimulatedKill
        original(self, run_id, entry)

    monkeypatch.setattr(RunStore, "log_review", kill_once)
    spec = _spec(generated_dataset, 25, review=True)
    with pytest.raises(SimulatedKill):
        await start_run(
            settings, spec, lines, RunOptions(deps=deps, reviewer=ScriptedReviewer(by_row=choose))
        )
    with sqlite3.connect(settings.db_path) as conn:
        [run_id] = conn.execute("SELECT run_id FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()

    trace = tmp_path / "resume.trace.jsonl"
    report = await resume_run(
        settings,
        run_id,
        RunOptions(deps=deps, reviewer=ScriptedReviewer(by_row=choose), trace_path=trace),
    )

    assert report.status == "completed"
    assert multiple.line.row_id not in _started_rows(trace, "oe_msrp")
    row = next(r for r in _rows(settings, run_id) if r.line.row_id == multiple.line.row_id)
    assert row.worksheet.current_oe == multiple.worksheet.oe_candidates[0]


async def test_a_rescored_row_is_judged_on_the_system_price_not_the_manual_one(
    settings: Settings, deps: AgentDeps, lines: list[RFQLine], generated_dataset: Path
) -> None:
    """Spec 13: a manual price only overlays the worksheet; confidence never sees it."""
    judged = _spec(generated_dataset, 25, strategy="evidence_plus_judge")
    clean = await start_run(settings, judged, lines, RunOptions(deps=deps))
    target = next(
        r
        for r in _rows(settings, clean.run_id)
        if r.line.row_id <= 25
        and r.line.our_part_no
        and r.worksheet.band == "yellow"
        and r.worksheet.selected_retail_price is not None
    )
    flaky = AgentDeps(
        lookup=deps.lookup,
        internal_db=FailingOnceDB(target.line.our_part_no or ""),
        tools=deps.tools,
    )
    manual: dict[int, Decide] = {
        target.line.row_id: lambda r: {"action": "modify_price", "retail": 1.23}
    }
    review = judged.model_copy(update={"review": True})
    broken = await start_run(
        settings, review, lines, RunOptions(deps=flaky, reviewer=ScriptedReviewer(by_row=manual))
    )
    briefs: list[str] = []

    def recording(request: FakeRequest) -> dict[str, Any]:
        briefs.append(request.text)
        return confidence_fakes.fake_judge(request)

    register_fake_handler("JudgeVerdict")(recording)
    try:
        await rerun_failed(
            settings, broken.run_id, RunOptions(deps=flaky, reviewer=ScriptedReviewer())
        )
    finally:
        register_fake_handler("JudgeVerdict")(confidence_fakes.fake_judge)

    [brief] = [b for b in briefs if target.line.customer_part_no in b]
    assert "1.23" not in brief
    row = next(r for r in _rows(settings, broken.run_id) if r.line.row_id == target.line.row_id)
    assert row.worksheet.selected_retail_price == 1.23  # still shown on the worksheet
    assert row.confidence == target.confidence


async def test_resume_after_a_killed_rerun_reruns_the_batches_not_reached_yet(
    settings: Settings, deps: AgentDeps, lines: list[RFQLine], generated_dataset: Path
) -> None:
    clean = await start_run(settings, _spec(generated_dataset, 25), lines, RunOptions(deps=deps))
    first = next(x for x in lines[:25] if x.our_part_no)
    second = next(x for x in lines[25:50] if x.our_part_no)
    flaky = AgentDeps(
        lookup=deps.lookup,
        internal_db=FailingOnceDB(first.our_part_no or "", second.our_part_no or ""),
        tools=deps.tools,
    )
    broken = await start_run(settings, _spec(generated_dataset, 25), lines, RunOptions(deps=flaky))
    failed = [r.line.row_id for r in _rows(settings, broken.run_id) if r.worksheet.failures]
    assert failed == [first.row_id, second.row_id]
    killer = KillOnArmDB()
    killer.armed = True  # dies inside the first rerun batch
    killing = AgentDeps(lookup=deps.lookup, internal_db=killer, tools=deps.tools)

    with pytest.raises(SimulatedKill):
        await rerun_failed(settings, broken.run_id, RunOptions(deps=killing))
    report = await resume_run(settings, broken.run_id, RunOptions(deps=deps))

    assert report.status == "completed"
    assert all(b.status == "completed" for b in report.batches)
    assert _rows(settings, broken.run_id) == _rows(settings, clean.run_id)


class TimingOutTool:
    """Wraps a tool; times out ``times`` times for one query, then behaves normally."""

    def __init__(self, inner: ToolProvider, query: str, times: int) -> None:
        self.name = inner.name
        self._inner = inner
        self._query = normalize_part_no(query)
        self._left = times

    async def search(self, query: str) -> list[ToolHit]:
        if normalize_part_no(query) == self._query and self._left > 0:
            self._left -= 1
            raise ToolTimeoutError(f"timeout for {query}")
        return await self._inner.search(query)


async def test_a_rerun_row_does_not_get_its_old_price_back_from_a_copy_in_a_later_batch(
    settings: Settings, deps: AgentDeps, generated_dataset: Path, tmp_path: Path
) -> None:
    all_lines = read_rfq(generated_dataset / "rfq_200.xlsx").lines
    clean = await start_run(
        settings, _spec(generated_dataset, 50), all_lines, RunOptions(deps=deps)
    )
    # A group leader whose later member sits in a later batch (it got a copy of the
    # leader's prices) and whose should_cost needs external pages (our part unknown).
    leader = next(
        r
        for r in _rows(settings, clean.run_id)
        if isinstance(r.oe, OEResolution)
        and r.oe.our_part_source == "none"
        and r.worksheet.shared_oe_rows
        and min(r.worksheet.shared_oe_rows) > r.line.row_id
        and max(r.worksheet.shared_oe_rows) > 50 * ((r.line.row_id - 1) // 50 + 1)
    )
    spec_tool = TimingOutTool(
        deps.tools["external_spec_search"], leader.worksheet.current_oe or "", times=2
    )
    flaky = AgentDeps(
        lookup=deps.lookup,
        internal_db=deps.internal_db,
        tools={**deps.tools, "external_spec_search": spec_tool},
    )
    broken = await start_run(
        settings, _spec(generated_dataset, 50), all_lines, RunOptions(deps=flaky)
    )
    failed = [r.line.row_id for r in _rows(settings, broken.run_id) if r.worksheet.failures]
    assert failed == [leader.line.row_id]

    trace = tmp_path / "rerun.trace.jsonl"
    await rerun_failed(settings, broken.run_id, RunOptions(deps=flaky, trace_path=trace))

    assert _started_rows(trace, "oe_msrp") == [leader.line.row_id]
    assert _started_rows(trace, "retail_price") == [leader.line.row_id]
    assert _rows(settings, broken.run_id) == _rows(settings, clean.run_id)


async def test_a_rerun_killed_during_a_paused_review_is_finalized_afresh_on_resume(
    settings: Settings, deps: AgentDeps, lines: list[RFQLine], generated_dataset: Path
) -> None:
    target = next(x for x in lines[:25] if x.our_part_no)
    flaky = AgentDeps(
        lookup=deps.lookup,
        internal_db=FailingOnceDB(target.our_part_no or ""),
        tools=deps.tools,
    )
    spec = _spec(generated_dataset, 25, review=True)
    quit_at_once = ScriptedReviewer([lambda r: None])
    paused = await start_run(settings, spec, lines, RunOptions(deps=flaky, reviewer=quit_at_once))
    assert paused.status == "awaiting_review"
    killer = KillOnArmDB()
    killer.armed = True
    killing = AgentDeps(lookup=deps.lookup, internal_db=killer, tools=deps.tools)

    with pytest.raises(SimulatedKill):
        await rerun_failed(settings, paused.run_id, RunOptions(deps=killing))
    shown: list[ResolvedRow] = []

    async def accept(prompt: ReviewPrompt) -> ReviewInput:
        shown.append(prompt.row)
        return ReviewInput(action="accept")

    report = await resume_run(settings, paused.run_id, RunOptions(deps=deps, reviewer=accept))

    assert report.status == "completed"
    # The review starts over on the rerun's results, not on the rows queued before it.
    assert shown and all(not r.worksheet.failures for r in shown)
    row = next(r for r in _rows(settings, paused.run_id) if r.line.row_id == target.row_id)
    assert row.worksheet.failures == []


# --- run lease (spec 13, phase-8 Q6) ------------------------------------------------------


def _create(settings: Settings, spec: RunSpec, lines: list[RFQLine]) -> str:
    store = RunStore.open(settings.db_path)
    try:
        return store.create_run(spec, lines)
    finally:
        store.close()


async def test_a_run_another_process_drives_cannot_be_resumed(
    settings: Settings, deps: AgentDeps, lines: list[RFQLine], generated_dataset: Path
) -> None:
    run_id = _create(settings, _spec(generated_dataset, 25), lines)
    store = RunStore.open(settings.db_path)
    store.acquire_lease(run_id, "api-worker", ttl_s=settings.storage.lease_ttl_s)

    with pytest.raises(LeaseHeld):
        await resume_run(settings, run_id, RunOptions(deps=deps))

    assert [(b.status, b.attempts) for b in store.batches(run_id)] == [("pending", 0)] * 3
    report = await resume_run(settings, run_id, RunOptions(deps=deps, lease_token="api-worker"))
    assert report.status == "completed"
    assert store.lease(run_id) is None  # released when the run finished
    store.close()


class LeaseThief:
    """Wraps a tool; on the first search another process takes the run's lease over."""

    def __init__(self, inner: ToolProvider, settings: Settings) -> None:
        self.name = inner.name
        self._inner = inner
        self._settings = settings
        self.run_id = ""

    async def search(self, query: str) -> list[ToolHit]:
        if self.run_id:
            store = RunStore.open(self._settings.db_path)
            far_future = time.time() + 10 * self._settings.storage.lease_ttl_s
            store.acquire_lease(self.run_id, "thief", ttl_s=1, now=far_future)
            store.close()
            self.run_id = ""
            await asyncio.sleep(0.2)  # the heartbeat notices meanwhile
        return await self._inner.search(query)


async def test_a_process_that_loses_its_lease_stops_driving_the_run(
    settings: Settings, deps: AgentDeps, lines: list[RFQLine], generated_dataset: Path
) -> None:
    fast = settings.model_copy(
        update={"storage": settings.storage.model_copy(update={"lease_heartbeat_s": 0.01})}
    )
    thief = LeaseThief(deps.tools["oem_catalog_search"], fast)
    tools = {**deps.tools, "oem_catalog_search": thief}
    thief_deps = AgentDeps(lookup=deps.lookup, internal_db=deps.internal_db, tools=tools)
    run_id = _create(fast, _spec(generated_dataset, 25), lines)
    thief.run_id = run_id

    with pytest.raises(LeaseLost):
        await resume_run(fast, run_id, RunOptions(deps=thief_deps))

    store = RunStore.open(fast.db_path)
    assert [b.status for b in store.batches(run_id)] == ["running", "pending", "pending"]
    assert store.run(run_id).status == "running"
    store.close()


async def test_a_lease_left_by_a_dead_process_on_this_host_is_taken_over_at_once(
    settings: Settings, deps: AgentDeps, lines: list[RFQLine], generated_dataset: Path
) -> None:
    run_id = _create(settings, _spec(generated_dataset, 25), lines)
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    store = RunStore.open(settings.db_path)
    store.acquire_lease(
        run_id, f"{gone.pid}@{socket.gethostname()}:dead", ttl_s=settings.storage.lease_ttl_s
    )
    store.close()

    report = await resume_run(settings, run_id, RunOptions(deps=deps))

    assert report.status == "completed"


async def test_the_checkpoint_store_uses_wal_and_waits_for_locks(tmp_path: Path) -> None:
    async with open_checkpointer(tmp_path / "checkpoints.db") as saver:
        await saver.setup()
        async with saver.conn.execute("PRAGMA journal_mode") as cur:
            assert (await cur.fetchone())[0] == "wal"
        async with saver.conn.execute("PRAGMA busy_timeout") as cur:
            assert (await cur.fetchone())[0] >= 10_000


# --- review on a finalized run (spec 13, phase-8 P1/P2) -----------------------------------


async def test_a_review_run_is_finalized_first_and_any_queued_row_can_be_answered(
    settings: Settings, deps: AgentDeps, lines: list[RFQLine], generated_dataset: Path
) -> None:
    plain = await start_run(settings, _spec(generated_dataset, 25), lines, RunOptions(deps=deps))
    red = [r.line.row_id for r in _rows(settings, plain.run_id) if r.worksheet.band == "red"]

    paused = await start_run(
        settings, _spec(generated_dataset, 25, review=True), lines, RunOptions(deps=deps)
    )

    assert paused.status == "awaiting_review"
    rows = _rows(settings, paused.run_id)
    assert len(rows) == 60 and all(r.confidence is not None for r in rows)
    assert await review_queue(settings, paused.run_id) == red

    last = red[-1]
    answered = await answer_review(
        settings,
        paused.run_id,
        ReviewDecision(row_id=last, action="reject", note="not ours"),
        RunOptions(deps=deps),
    )

    assert answered.status == "awaiting_review"
    assert await review_queue(settings, paused.run_id) == red[:-1]
    shown = {r.line.row_id: r for r in _rows(settings, paused.run_id)}
    assert shown[last].worksheet.review_status == "rejected"
    with pytest.raises(NotInReviewQueue):
        await answer_review(
            settings,
            paused.run_id,
            ReviewDecision(row_id=last, action="accept"),
            RunOptions(deps=deps),
        )

    for rid in red[:-1]:
        report = await answer_review(
            settings,
            paused.run_id,
            ReviewDecision(row_id=rid, action="accept"),
            RunOptions(deps=deps),
        )
    assert report.status == "completed"
    assert await review_queue(settings, paused.run_id) == []


async def test_a_run_without_review_cannot_be_reviewed(
    settings: Settings, deps: AgentDeps, lines: list[RFQLine], generated_dataset: Path
) -> None:
    report = await start_run(settings, _spec(generated_dataset, 25), lines, RunOptions(deps=deps))
    red = next(r.line.row_id for r in _rows(settings, report.run_id) if r.worksheet.band == "red")

    assert report.status == "completed"
    with pytest.raises(ReviewNotOpen):
        await answer_review(
            settings,
            report.run_id,
            ReviewDecision(row_id=red, action="accept"),
            RunOptions(deps=deps),
        )


def _quick_lease(settings: Settings) -> Settings:
    storage = settings.storage.model_copy(update={"lease_ttl_s": 0.4, "lease_heartbeat_s": 0.05})
    return settings.model_copy(update={"storage": storage})


class BlockingTool:
    """Wraps a tool; the first search blocks the event loop longer than the lease TTL while
    another process tries to take the run over."""

    def __init__(self, inner: ToolProvider, settings: Settings) -> None:
        self.name = inner.name
        self._inner = inner
        self._settings = settings
        self.run_id = ""
        self.thief_errors: list[BaseException] = []

    def _steal(self) -> None:
        time.sleep(0.6)  # past the TTL, while the loop is still blocked
        store = RunStore.open(self._settings.db_path)
        try:
            store.acquire_lease(self.run_id, "thief", ttl_s=self._settings.storage.lease_ttl_s)
        except LeaseHeld as exc:
            self.thief_errors.append(exc)
        finally:
            store.close()

    async def search(self, query: str) -> list[ToolHit]:
        if self.run_id:
            thief = threading.Thread(target=self._steal)
            thief.start()
            time.sleep(1.0)  # synchronous work: the event loop cannot run
            thief.join()
            self.run_id = ""
        return await self._inner.search(query)


async def test_the_lease_stays_fresh_while_the_event_loop_is_blocked(
    settings: Settings, deps: AgentDeps, lines: list[RFQLine], generated_dataset: Path
) -> None:
    quick = _quick_lease(settings)
    blocking = BlockingTool(deps.tools["oem_catalog_search"], quick)
    tools = {**deps.tools, "oem_catalog_search": blocking}
    run_id = _create(quick, _spec(generated_dataset, 25), lines)
    blocking.run_id = run_id

    report = await resume_run(
        quick,
        run_id,
        RunOptions(deps=AgentDeps(lookup=deps.lookup, internal_db=deps.internal_db, tools=tools)),
    )

    assert len(blocking.thief_errors) == 1  # the thief found the lease held
    assert report.status == "completed"


async def test_a_heartbeat_that_keeps_failing_stops_driving_the_run(
    settings: Settings,
    deps: AgentDeps,
    lines: list[RFQLine],
    generated_dataset: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quick = _quick_lease(settings)
    run_id = _create(quick, _spec(generated_dataset, 25), lines)

    def locked(self: RunStore, run_id: str, owner: str, *, now: float | None = None) -> bool:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(RunStore, "renew_lease", locked)

    with pytest.raises(LeaseLost):
        await resume_run(quick, run_id, RunOptions(deps=deps))


async def test_a_new_oe_number_killed_midway_is_finished_by_answering_the_row_again(
    settings: Settings, deps: AgentDeps, lines: list[RFQLine], generated_dataset: Path
) -> None:
    paused = await start_run(
        settings, _spec(generated_dataset, 25, review=True), lines, RunOptions(deps=deps)
    )
    rid = (await review_queue(settings, paused.run_id))[0]
    decision = ReviewDecision(row_id=rid, action="modify_oe", oe="manual-9")
    killer = KillOnArmDB()
    killer.armed = True
    killing = AgentDeps(lookup=deps.lookup, internal_db=killer, tools=deps.tools)

    with pytest.raises(SimulatedKill):
        await answer_review(settings, paused.run_id, decision, RunOptions(deps=killing))

    assert rid in await review_queue(settings, paused.run_id)
    await answer_review(settings, paused.run_id, decision, RunOptions(deps=deps))
    row = next(r for r in _rows(settings, paused.run_id) if r.line.row_id == rid)
    assert (row.worksheet.review_status, row.worksheet.current_oe) == ("modified", "MANUAL-9")
    store = RunStore.open(settings.db_path)
    assert [e.row_id for e in store.reviews(paused.run_id)] == [rid]
    store.close()


class LockHoldingTool:
    """Wraps a tool; the first search starts another process-like writer holding the result
    store's write lock well past the lease TTL."""

    def __init__(self, inner: ToolProvider, db_path: Path, hold_s: float) -> None:
        self.name = inner.name
        self._inner = inner
        self._db_path = db_path
        self._hold_s = hold_s
        self.armed = False

    def _hold(self) -> None:
        conn = sqlite3.connect(self._db_path, timeout=30)
        conn.execute("BEGIN EXCLUSIVE")
        time.sleep(self._hold_s)
        conn.rollback()
        conn.close()

    async def search(self, query: str) -> list[ToolHit]:
        if self.armed:
            self.armed = False
            threading.Thread(target=self._hold, daemon=True).start()
        return await self._inner.search(query)


async def test_a_heartbeat_stuck_on_a_lock_gives_the_run_up_before_its_lease_expires(
    settings: Settings, deps: AgentDeps, lines: list[RFQLine], generated_dataset: Path
) -> None:
    # A refresh waiting on SQLite's lock must not outlive the lease: otherwise another
    # process may take the run over while this one still drives it.
    quick = _quick_lease(settings)
    holder = LockHoldingTool(deps.tools["oem_catalog_search"], quick.db_path, hold_s=1.5)
    holder.armed = True
    tools = {**deps.tools, "oem_catalog_search": holder}
    run_id = _create(quick, _spec(generated_dataset, 25), lines)

    with pytest.raises(LeaseLost):
        await resume_run(
            quick,
            run_id,
            RunOptions(
                deps=AgentDeps(lookup=deps.lookup, internal_db=deps.internal_db, tools=tools)
            ),
        )


async def test_a_lease_lost_while_the_block_finishes_is_reported_not_left_as_a_cancel(
    settings: Settings, lines: list[RFQLine], generated_dataset: Path
) -> None:
    run_id = _create(settings, _spec(generated_dataset, 25), lines)
    store = RunStore.open(settings.db_path)

    with pytest.raises(LeaseLost):
        async with hold_lease(store, run_id, "me", ttl_s=1.0, heartbeat_s=0.01):
            other = RunStore.open(settings.db_path)
            other.acquire_lease(run_id, "thief", ttl_s=1.0, now=time.time() + 10)
            other.close()
            time.sleep(0.2)  # synchronous: the block ends without awaiting again

    await asyncio.sleep(0.05)  # no stray cancellation reaches the caller afterwards
    store.close()
