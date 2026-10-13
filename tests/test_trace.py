from __future__ import annotations

import threading
from pathlib import Path

from rfq_agent.trace import TraceWriter, load_trace, render_row


def test_writer_is_thread_safe_and_ordered(tmp_path: Path) -> None:
    writer = TraceWriter(tmp_path / "t.trace.jsonl", "run-1")

    def worker(row: int) -> None:
        for i in range(50):
            writer.event(row, "agent", "step", i=i)

    threads = [threading.Thread(target=worker, args=(r,)) for r in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    events = load_trace(writer.path)
    assert len(events) == 200
    assert sorted(e["seq"] for e in events) == list(range(1, 201))
    assert all(e["run_id"] == "run-1" for e in events)


def test_render_row_truncates_unless_full(tmp_path: Path) -> None:
    writer = TraceWriter(tmp_path / "t.trace.jsonl", "r")
    writer.event(
        7,
        "oe",
        "llm_call",
        prompt=[{"role": "human", "content": "x" * 2000}],
        response={"content": "", "tool_calls": [{"name": "S", "args": {"a": 1}}]},
    )
    events = load_trace(writer.path)
    short = render_row(events, 7)
    assert "use --full" in short and "◂ S:" in short
    assert "use --full" not in render_row(events, 7, full=True)
    assert render_row(events, 99) == "(no events for row 99)"
