"""Page-test fixtures: the dashboard drives the real API in-process over a fake-mode run."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from dashboard.api_client import ApiClient
from rfq_agent.api import create_app
from rfq_agent.config import Settings, get_settings, load_settings
from rfq_agent.eval.run_eval import evaluate_run
from rfq_agent.schemas import RunDetail, RunProgress, RunRecord, RunSpec
from rfq_agent.store.runs import open_store
from tests.test_api import ThreadLauncher

TIMEOUT = 30  # the first script run imports pandas / altair cold


@dataclass
class Backend:
    """The API in this process; its workers run in threads (``launcher.join`` waits)."""

    api: ApiClient
    launcher: ThreadLauncher
    settings: Settings
    sample: bytes

    def finished_run(self, *, review: bool = True) -> str:
        record = self.api.create_run("rfq_sample_20.xlsx", self.sample, review=review, batch_size=7)
        self.launcher.join()
        run_id: str = record["run_id"]
        return run_id

    def evaluate(self, run_id: str) -> None:
        """What ``rfq eval --run`` stores (the dashboard only reads it over HTTP)."""
        with open_store(self.settings.db_path) as store:
            evaluate_run(store, run_id)


@pytest.fixture
def backend(config_dir: Path, generated_dataset: Path) -> Iterator[Backend]:
    path = config_dir / "settings.yaml"
    data = yaml.safe_load(path.read_text())
    data["data"]["dataset_dir"] = str(generated_dataset)
    path.write_text(yaml.safe_dump(data))
    get_settings.cache_clear()
    settings = load_settings(config_dir)
    launcher = ThreadLauncher()
    with TestClient(create_app(settings, launcher)) as http:
        sample = (generated_dataset / "rfq_sample_20.xlsx").read_bytes()
        yield Backend(ApiClient(http), launcher, settings, sample)


def _view_script(view: str) -> None:
    # AppTest runs this function's source as the script: imports must stay inside.
    from dashboard.app import render_view

    render_view(view)


def open_page(view: str, api: ApiClient, **state: Any) -> AppTest:
    """Run one dashboard page with ``api`` injected and ``state`` preset."""
    at = AppTest.from_function(_view_script, kwargs={"view": view}, default_timeout=TIMEOUT)
    at.session_state["api_client"] = api
    for key, value in state.items():
        at.session_state[key] = value
    return at.run()


def texts(at: AppTest) -> str:
    """Every piece of text the page shows, for loose content checks."""
    parts = []
    for kind in ("title", "header", "subheader", "markdown", "caption", "text", "info"):
        parts += [str(e.value) for e in getattr(at, kind)]
    parts += [str(e.value) for e in at.success] + [str(e.value) for e in at.warning]
    parts += [str(e.value) for e in at.error]
    parts += [f"{m.label} {m.value}" for m in at.metric]
    return "\n".join(parts)


Route = Callable[[httpx.Request], httpx.Response]


def stub_api(routes: dict[tuple[str, str], Route | Any]) -> tuple[ApiClient, list[httpx.Request]]:
    """A client whose API answers ``(method, path)`` from ``routes``: a handler or JSON (200)."""
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        request.read()
        seen.append(request)
        route = routes.get((request.method, request.url.path))
        if route is None:
            return httpx.Response(404, json={"detail": f"no route {request.url.path}"})
        if callable(route):
            response: httpx.Response = route(request)
            return response
        return httpx.Response(200, json=route)

    http = httpx.Client(base_url="http://api.test", transport=httpx.MockTransport(answer))
    return ApiClient(http), seen


def run_detail(
    status: str = "running", *, lease_active: bool = True, done: int = 1, total: int = 3
) -> dict[str, Any]:
    """``RunDetail`` JSON for run ``r1`` built from the backend's own models."""
    spec = RunSpec(input="x.xlsx", dataset_dir="d", batch_size=7, strategy="evidence", review=True)
    record = RunRecord(
        run_id="r1", created_at="2026-10-08T00:00:00+00:00", spec=spec, status=status
    )
    progress = RunProgress(
        batches_total=total, pending=total - done, running=0, completed=done, failed=0
    )
    detail = RunDetail(run=record, progress=progress, summary=None, lease_active=lease_active)
    return detail.model_dump(mode="json")
