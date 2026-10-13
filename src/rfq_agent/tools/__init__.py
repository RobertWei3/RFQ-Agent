"""Mock external data sources (spec section 9). Real implementations can replace them later."""

from rfq_agent.tools.base import ToolHit, ToolProvider, ToolTimeoutError
from rfq_agent.tools.registry import TOOL_CORPORA, build_tools

__all__ = ["TOOL_CORPORA", "ToolHit", "ToolProvider", "ToolTimeoutError", "build_tools"]
