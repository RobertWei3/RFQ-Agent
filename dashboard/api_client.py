"""HTTP client for the RFQ-Agent API (spec 15.1); responses stay plain JSON."""

from __future__ import annotations

from typing import Any

import httpx

Json = Any
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class ApiError(Exception):
    """A request the API refused or could not answer; ``status`` 0 means no connection."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


class ApiClient:
    """One method per endpoint the dashboard uses."""

    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    def list_runs(self) -> list[Json]:
        result: list[Json] = self._json("GET", "/runs")
        return result

    def run(self, run_id: str) -> Json:
        return self._json("GET", f"/runs/{run_id}")

    def create_run(
        self,
        filename: str,
        content: bytes,
        *,
        strategy: str | None = None,
        batch_size: int | None = None,
        review: bool = False,
        limit: int | None = None,
    ) -> Json:
        """Upload an RFQ workbook; the API starts the run in the background (202)."""
        form = {"strategy": strategy, "batch_size": batch_size, "limit": limit}
        data = {k: str(v) for k, v in form.items() if v is not None}
        if review:
            data["review"] = "true"
        files = {"file": (filename, content, XLSX)}
        return self._json("POST", "/runs", data=data, files=files)

    def resume(self, run_id: str) -> Json:
        return self._json("POST", f"/runs/{run_id}/resume")

    def rows(
        self,
        run_id: str,
        *,
        bands: list[str] | None = None,
        statuses: list[str] | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> Json:
        """One page of worksheet rows, filtered by band and review status."""
        params: list[tuple[str, str | int]] = [("band", b) for b in bands or []]
        params += [("status", s) for s in statuses or []]
        params += [("offset", offset), ("limit", limit)]
        return self._json("GET", f"/runs/{run_id}/rows", params=params)

    def row(self, run_id: str, row_id: int) -> Json:
        return self._json("GET", f"/runs/{run_id}/rows/{row_id}")

    def review(
        self,
        run_id: str,
        row_id: int,
        action: str,
        *,
        oe: str | None = None,
        msrp: float | None = None,
        retail: float | None = None,
        note: str | None = None,
    ) -> Json:
        """Answer a queued red row; ``done`` is False when a worker finishes it (202)."""
        fields = {"action": action, "oe": oe, "msrp": msrp, "retail": retail, "note": note}
        body = {k: v for k, v in fields.items() if v is not None}
        return self._json("POST", f"/runs/{run_id}/rows/{row_id}/review", json=body)

    def worksheet(self, run_id: str) -> bytes:
        return self._send("GET", f"/runs/{run_id}/worksheet.xlsx").content

    def evaluation(self, run_id: str) -> Json | None:
        """The run's evaluation, or None while it has none (404)."""
        try:
            return self._json("GET", f"/runs/{run_id}/eval")
        except ApiError as exc:
            if exc.status == 404:
                return None
            raise

    def _json(self, method: str, path: str, **kwargs: Any) -> Json:
        return self._send(method, path, **kwargs).json()

    def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.TransportError as exc:
            raise ApiError(0, f"无法连接 API {self._http.base_url}：{exc}") from None
        if response.is_error:
            raise ApiError(response.status_code, _detail(response))
        return response


def _detail(response: httpx.Response) -> str:
    """FastAPI's ``detail``: a message, or a list of validation errors."""
    try:
        detail = response.json()["detail"]
    except (ValueError, KeyError, TypeError):
        return response.text
    if isinstance(detail, list):
        return "; ".join(
            f"{'.'.join(str(p) for p in e.get('loc', []))}: {e.get('msg', '')}" for e in detail
        )
    return str(detail)
