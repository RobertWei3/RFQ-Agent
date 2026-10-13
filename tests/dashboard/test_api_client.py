"""``dashboard.api_client``: the dashboard's only way to the backend (spec 15.1)."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from dashboard.api_client import ApiClient, ApiError

Handler = Callable[[httpx.Request], httpx.Response]


def _client(handler: Handler) -> tuple[ApiClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        request.read()
        seen.append(request)
        return handler(request)

    http = httpx.Client(base_url="http://api.test", transport=httpx.MockTransport(record))
    return ApiClient(http), seen


def test_runs_and_a_run_are_read_by_id() -> None:
    client, seen = _client(lambda r: httpx.Response(200, json=[{"run_id": "r1"}]))
    assert client.list_runs() == [{"run_id": "r1"}]
    client, seen2 = _client(lambda r: httpx.Response(200, json={"run": {"run_id": "r1"}}))
    assert client.run("r1") == {"run": {"run_id": "r1"}}
    assert [(r.method, r.url.path) for r in seen + seen2] == [
        ("GET", "/runs"),
        ("GET", "/runs/r1"),
    ]


@pytest.mark.parametrize(
    ("response", "status", "detail"),
    [
        (httpx.Response(404, json={"detail": "unknown run 'x'"}), 404, "unknown run 'x'"),
        (httpx.Response(409, json={"detail": "run x is completed"}), 409, "run x is completed"),
        (
            httpx.Response(422, json={"detail": [{"loc": ["body", "file"], "msg": "required"}]}),
            422,
            "body.file: required",
        ),
        (httpx.Response(500, text="Internal Server Error"), 500, "Internal Server Error"),
    ],
)
def test_a_refused_request_raises_the_servers_detail(
    response: httpx.Response, status: int, detail: str
) -> None:
    client, _ = _client(lambda r: response)
    with pytest.raises(ApiError) as caught:
        client.run("x")
    assert (caught.value.status, caught.value.detail) == (status, detail)


def test_an_unreachable_api_is_reported_with_its_address() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client, _ = _client(refuse)
    with pytest.raises(ApiError) as caught:
        client.list_runs()
    assert caught.value.status == 0
    assert "http://api.test" in caught.value.detail


def _form(request: httpx.Request) -> dict[str, str]:
    """Plain (non-file) fields of a multipart body."""
    body = request.content.decode("latin-1")
    fields: dict[str, str] = {}
    for part in body.split("--" + request.headers["content-type"].split("boundary=")[1]):
        if 'name="' in part and "filename=" not in part:
            name = part.split('name="')[1].split('"')[0]
            fields[name] = part.split("\r\n\r\n", 1)[1].rstrip("\r\n")
    return fields


def test_an_upload_sends_the_workbook_and_only_the_options_given() -> None:
    client, seen = _client(lambda r: httpx.Response(202, json={"run_id": "r9"}))
    record = client.create_run("rfq.xlsx", b"PK-bytes", review=True, batch_size=7)
    assert record == {"run_id": "r9"}
    request = seen[0]
    assert (request.method, request.url.path) == ("POST", "/runs")
    assert b'filename="rfq.xlsx"' in request.content
    assert b"PK-bytes" in request.content
    assert _form(request) == {"review": "true", "batch_size": "7"}


def test_rows_pass_filters_as_repeated_query_parameters() -> None:
    client, seen = _client(lambda r: httpx.Response(200, json={"total": 0, "items": []}))
    client.rows("r1", bands=["red", "yellow"], statuses=["unreviewed"], offset=100, limit=50)
    client.row("r1", 7)
    assert seen[0].url.path == "/runs/r1/rows"
    assert seen[0].url.params.multi_items() == [
        ("band", "red"),
        ("band", "yellow"),
        ("status", "unreviewed"),
        ("offset", "100"),
        ("limit", "50"),
    ]
    assert seen[1].url.path == "/runs/r1/rows/7"


def test_a_review_posts_only_the_fields_given() -> None:
    client, seen = _client(lambda r: httpx.Response(202, json={"done": False}))
    assert client.review("r1", 3, "modify_oe", oe="NEW-9") == {"done": False}
    assert (seen[0].method, seen[0].url.path) == ("POST", "/runs/r1/rows/3/review")
    assert json.loads(seen[0].content) == {"action": "modify_oe", "oe": "NEW-9"}


def test_resume_worksheet_and_evaluation() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/resume"):
            return httpx.Response(202, json={"run_id": "r1"})
        if request.url.path.endswith("/worksheet.xlsx"):
            return httpx.Response(200, content=b"xlsx-bytes")
        return httpx.Response(404, json={"detail": "no evaluation for run r1 yet"})

    client, seen = _client(answer)
    assert client.resume("r1") == {"run_id": "r1"}
    assert client.worksheet("r1") == b"xlsx-bytes"
    assert client.evaluation("r1") is None
    assert [(r.method, r.url.path) for r in seen] == [
        ("POST", "/runs/r1/resume"),
        ("GET", "/runs/r1/worksheet.xlsx"),
        ("GET", "/runs/r1/eval"),
    ]
