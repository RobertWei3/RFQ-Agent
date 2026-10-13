"""FastAPI app (spec 15.1): runs, rows, review, worksheet; responses are spec 7 models."""

from __future__ import annotations

import io
import uuid
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from openpyxl.utils.exceptions import InvalidFileException

from rfq_agent.api.launcher import Launcher, SubprocessLauncher
from rfq_agent.config import Settings, resolve_path
from rfq_agent.intake import IntakeError, read_rfq
from rfq_agent.orchestrator.estimate import Calls, real_calls, review_calls, run_calls
from rfq_agent.orchestrator.lease import acquire, is_driven, new_lease_owner
from rfq_agent.orchestrator.run import (
    NotInReviewQueue,
    ReviewNotOpen,
    answer_review,
    ensure_reviewable,
    review_queue,
)
from rfq_agent.output.excel import build_workbook
from rfq_agent.output.summary import run_summary
from rfq_agent.schemas import (
    OPEN_BATCH_STATUSES,
    Band,
    ResolvedRow,
    ReviewDecision,
    ReviewFilter,
    ReviewInput,
    ReviewOutcome,
    RowDetail,
    RowPage,
    RunDetail,
    RunEval,
    RunRecord,
    RunSpec,
    StrategyName,
)
from rfq_agent.store.runs import LeaseHeld, RunStore, open_store

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def create_app(
    settings: Settings, launcher: Launcher | None = None, *, allow_real: bool = False
) -> FastAPI:
    """The API over ``settings``' result store; ``launcher`` starts worker processes."""
    app = FastAPI(title="RFQ-Agent", version="0.1.0")
    app.state.api = ApiContext(
        settings, launcher or SubprocessLauncher(settings.config_dir), allow_real
    )
    _routes(app)
    return app


class ApiContext:
    """What the routes share: settings, the worker launcher, the real-LLM switch."""

    def __init__(self, settings: Settings, launcher: Launcher, allow_real: bool) -> None:
        self.settings = settings
        self.launcher = launcher
        self.allow_real = allow_real
        self.runs_dir = settings.db_path.parent / "runs"

    @contextmanager
    def store(self) -> Iterator[RunStore]:
        with open_store(self.settings.db_path) as store:
            yield store

    def record(self, store: RunStore, run_id: str) -> RunRecord:
        try:
            return store.run(run_id)
        except KeyError:
            raise HTTPException(404, f"unknown run {run_id!r}") from None

    def guard_real(self, calls: Calls) -> None:
        """409 when this work would call a real provider and the server may not."""
        paid = real_calls(self.settings, calls)
        if paid and not self.allow_real:
            expected = ", ".join(f"{role} ~{n.expected}" for role, n in paid.items())
            raise HTTPException(
                409,
                f"real LLM provider for {expected} (expected calls); "
                "start the server with --allow-real to permit paid calls",
            )

    def row(self, store: RunStore, run_id: str, row_id: int) -> ResolvedRow:
        found = store.rows(run_id, [row_id])
        if not found:
            raise HTTPException(404, f"run {run_id} has no row {row_id}")
        return found[0]

    def run_in_worker(self, store: RunStore, run_id: str, argv: list[str]) -> None:
        """Take the run's lease (409 if another process drives it) and hand it to a worker
        process running ``rfq <argv>``."""
        token = new_lease_owner()
        try:
            acquire(store, run_id, token, ttl_s=self.settings.storage.lease_ttl_s)
        except LeaseHeld as exc:
            raise HTTPException(409, str(exc)) from None
        extra = ["--allow-real"] if self.allow_real else []
        log = self.runs_dir / f"run_{run_id}.worker.log"
        self.launcher.launch([*argv, "--lease-token", token, *extra], log)


def _api(request: Request) -> ApiContext:
    api: ApiContext = request.app.state.api
    return api


def _routes(app: FastAPI) -> None:
    app.post("/runs", status_code=202, response_model=RunRecord)(create_run)
    app.get("/runs", response_model=list[RunRecord])(list_runs)
    app.get("/runs/{run_id}", response_model=RunDetail)(get_run)
    app.get("/runs/{run_id}/rows", response_model=RowPage)(list_rows)
    app.get("/runs/{run_id}/rows/{row_id}", response_model=RowDetail)(get_row)
    app.post(
        "/runs/{run_id}/rows/{row_id}/review",
        response_model=ReviewOutcome,
        responses={202: {"model": ReviewOutcome, "description": "Accepted; a worker finishes"}},
    )(review_row)
    app.get(
        "/runs/{run_id}/worksheet.xlsx",
        response_class=Response,
        responses={200: {"content": {XLSX: {}}, "description": "Pricing worksheet"}},
    )(download_worksheet)
    app.get(
        "/runs/{run_id}/eval",
        response_model=RunEval,
        responses={404: {"description": "Unknown run, or not evaluated yet (rfq eval)"}},
    )(get_eval)
    app.post("/runs/{run_id}/resume", status_code=202, response_model=RunRecord)(resume)


# --- runs ---------------------------------------------------------------------------------


async def create_run(
    request: Request,
    file: Annotated[UploadFile, File(description="RFQ workbook (.xlsx).")],
    strategy: Annotated[StrategyName | None, Form()] = None,
    batch_size: Annotated[int | None, Form(ge=1)] = None,
    review: Annotated[bool, Form()] = False,
    limit: Annotated[int | None, Form(ge=1)] = None,
    dataset: Annotated[str | None, Form(description="Dataset dir on the server.")] = None,
) -> RunRecord:
    """Upload an RFQ and start a run in a background worker (spec 13)."""
    api = _api(request)
    settings = api.settings
    path = await _save_upload(api, file)
    try:
        lines = read_rfq(path).lines
    except (IntakeError, InvalidFileException, zipfile.BadZipFile, KeyError, ValueError) as exc:
        path.unlink(missing_ok=True)
        raise HTTPException(422, f"cannot read the RFQ workbook: {exc}") from None
    lines = lines[:limit] if limit else lines
    spec = RunSpec(
        input=str(path),
        dataset_dir=str(resolve_path(Path(dataset)) if dataset else settings.dataset_dir),
        batch_size=batch_size or settings.pipeline.batch_size,
        strategy=strategy or settings.confidence.strategy,
        review=review,
    )
    calls = run_calls(settings, spec.strategy, len(lines))
    try:
        api.guard_real(calls)
    except HTTPException:
        path.unlink(missing_ok=True)
        raise
    with api.store() as store:
        run_id = store.create_run(spec, lines)
        api.run_in_worker(store, run_id, _resume_argv(api, run_id))
        return store.run(run_id)


async def _save_upload(api: ApiContext, file: UploadFile) -> Path:
    name = Path(file.filename or "rfq.xlsx").name
    if Path(name).suffix.lower() != ".xlsx":
        raise HTTPException(422, f"expected an .xlsx workbook, got {name!r}")
    target = api.settings.upload_dir / f"{uuid.uuid4().hex[:12]}_{name}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(await file.read())
    return target


def _resume_argv(api: ApiContext, run_id: str) -> list[str]:
    return ["run", "--resume", run_id, "--worker", "--output-dir", str(api.runs_dir)]


def list_runs(request: Request) -> list[RunRecord]:
    with _api(request).store() as store:
        return store.list_runs()


def get_run(request: Request, run_id: str) -> RunDetail:
    """Status, batch progress, summary once finalized, and whether a process drives it."""
    api = _api(request)
    with api.store() as store:
        record = api.record(store, run_id)
        return RunDetail(
            run=record,
            progress=store.progress(run_id),
            summary=run_summary(store, run_id),
            lease_active=is_driven(store, run_id, ttl_s=api.settings.storage.lease_ttl_s),
        )


def resume(request: Request, run_id: str) -> RunRecord:
    """Start a worker for a run that stopped midway (its worker was killed)."""
    api = _api(request)
    with api.store() as store:
        record = api.record(store, run_id)
        if record.status != "running":
            raise HTTPException(409, f"run {run_id} is {record.status}: nothing to resume")
        rows = sum(
            len(b.rows_to_run()) for b in store.batches(run_id) if b.status in OPEN_BATCH_STATUSES
        )
        api.guard_real(run_calls(api.settings, record.spec.strategy, rows))
        api.run_in_worker(store, run_id, _resume_argv(api, run_id))
        return record


# --- rows -----------------------------------------------------------------------------------


def list_rows(
    request: Request,
    run_id: str,
    band: Annotated[list[Band] | None, Query()] = None,
    status: Annotated[list[ReviewFilter] | None, Query(description="Review status.")] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> RowPage:
    api = _api(request)
    with api.store() as store:
        api.record(store, run_id)
        return store.row_page(
            run_id, bands=band or [], statuses=status or [], offset=offset, limit=limit
        )


async def get_row(request: Request, run_id: str, row_id: int) -> RowDetail:
    """Everything about one row: all stage results, evidence, reasoning, reviews."""
    api = _api(request)
    with api.store() as store:
        api.record(store, run_id)
        row = api.row(store, run_id, row_id)
        reviews = store.reviews(run_id, row_id=row_id)
    queued = row_id in await review_queue(api.settings, run_id)
    return RowDetail(row=row, reviews=reviews, in_review_queue=queued)


async def review_row(
    request: Request, response: Response, run_id: str, row_id: int, body: ReviewInput
) -> ReviewOutcome:
    """Answer one queued red row; a new OE number reruns agents in a worker (202)."""
    api = _api(request)
    with api.store() as store:
        record = api.record(store, run_id)
        api.row(store, run_id, row_id)
    decision = ReviewDecision.for_row(row_id, body)
    try:
        ensure_reviewable(record, await review_queue(api.settings, run_id), row_id)
        if decision.action == "modify_oe":
            api.guard_real(review_calls(api.settings, record.spec.strategy))
            argv = ["runs", "answer", run_id, "--decision", decision.model_dump_json()]
            with api.store() as store:
                api.run_in_worker(store, run_id, argv)
            response.status_code = 202
            return ReviewOutcome(run_id=run_id, row_id=row_id, done=False)
        await answer_review(api.settings, run_id, decision)
    except (LeaseHeld, ReviewNotOpen, NotInReviewQueue) as exc:
        raise HTTPException(409, str(exc)) from None
    with api.store() as store:
        row = api.row(store, run_id, row_id)
    return ReviewOutcome(run_id=run_id, row_id=row_id, done=True, row=row)


# --- worksheet and evaluation -----------------------------------------------------------------


def download_worksheet(request: Request, run_id: str) -> Response:
    """The pricing worksheet, built now from the stored rows (latest reviews included)."""
    api = _api(request)
    with api.store() as store:
        record = api.record(store, run_id)
        summary = run_summary(store, run_id)
        if summary is None:
            raise HTTPException(409, f"run {run_id} is not finalized yet")
        rows = store.rows(run_id)
    buffer = io.BytesIO()
    build_workbook(record, rows, summary).save(buffer)
    return Response(
        buffer.getvalue(),
        media_type=XLSX,
        headers={"Content-Disposition": f'attachment; filename="run_{run_id}.xlsx"'},
    )


def get_eval(request: Request, run_id: str) -> RunEval:
    """The run's stored evaluation (spec 14); ``rfq eval --run`` makes it."""
    api = _api(request)
    with api.store() as store:
        api.record(store, run_id)
        found = store.evaluation(run_id)
    if found is None:
        raise HTTPException(404, f"no evaluation for run {run_id} yet (rfq eval --run {run_id})")
    return found
