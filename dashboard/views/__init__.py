"""The six dashboard pages (spec 15.2); each module has ``TITLE`` and ``render(api)``."""

from __future__ import annotations

import os
from types import ModuleType

import httpx
import streamlit as st

from dashboard.api_client import ApiClient
from dashboard.views import evaluation, overview, review, row_detail, upload, worksheet

API_URL_ENV = "RFQ_API_URL"
DEFAULT_API_URL = "http://127.0.0.1:8000"

VIEWS: dict[str, ModuleType] = {
    "upload": upload,
    "overview": overview,
    "worksheet": worksheet,
    "row": row_detail,
    "review": review,
    "evaluation": evaluation,
}


def api_client() -> ApiClient:
    """The session's API client: injected by tests, else one for ``RFQ_API_URL``."""
    if "api_client" not in st.session_state:
        url = os.environ.get(API_URL_ENV, DEFAULT_API_URL)
        st.session_state["api_client"] = ApiClient(httpx.Client(base_url=url, timeout=30))
    client: ApiClient = st.session_state["api_client"]
    return client
