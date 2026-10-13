"""HTTP API (spec 15.1); the dashboard reaches the backend only through it."""

from rfq_agent.api.app import create_app

__all__ = ["create_app"]
