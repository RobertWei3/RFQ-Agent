"""Dashboard entry point: ``streamlit run dashboard/app.py`` (spec 15.2)."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

# ``streamlit run`` puts this file's folder on sys.path, not the project root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import streamlit as st  # noqa: E402

from dashboard.views import VIEWS, api_client  # noqa: E402


def render_view(view: str) -> None:
    """Render one page on its own (what each navigation entry runs)."""
    VIEWS[view].render(api_client())


def main() -> None:
    """Render the dashboard: the run picker in the sidebar and the six pages."""
    st.set_page_config(page_title="RFQ-Agent", layout="wide")
    from dashboard.views import common

    pages = {
        key: st.Page(_page(key), title=view.TITLE, url_path=key, default=key == "upload")
        for key, view in VIEWS.items()
    }
    st.session_state["pages"] = pages
    navigation = st.navigation(list(pages.values()))
    common.run_picker(api_client())
    navigation.run()


def _page(key: str) -> Callable[[], None]:
    """A named function for ``st.Page`` (it titles and routes pages by function)."""

    def page() -> None:
        render_view(key)

    page.__name__ = key
    return page


if __name__ == "__main__":
    main()
