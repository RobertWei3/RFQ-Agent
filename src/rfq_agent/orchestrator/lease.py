"""Run lease (spec 13): only one process drives a run at a time."""

from __future__ import annotations

import asyncio
import os
import socket
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from rfq_agent.store.runs import LeaseHeld, RunStore

# Longest a heartbeat waits for SQLite's lock (never longer than the heartbeat interval).
HEARTBEAT_LOCK_WAIT_S = 2.0


class LeaseLost(RuntimeError):
    """Another process took the run over after this one's heartbeat expired."""


def new_lease_owner() -> str:
    """An opaque token naming this process (pid, host) plus a random part."""
    return f"{os.getpid()}@{socket.gethostname()}:{uuid.uuid4().hex[:8]}"


def acquire(store: RunStore, run_id: str, owner: str, *, ttl_s: float) -> None:
    """Take the lease; one left by a dead process on this host is taken over at once."""
    try:
        store.acquire_lease(run_id, owner, ttl_s=ttl_s)
    except LeaseHeld as held:
        if not (
            _dead_on_this_host(held.owner) and store.take_over_lease(run_id, held.owner, owner)
        ):
            raise


def is_driven(store: RunStore, run_id: str, *, ttl_s: float) -> bool:
    """A live process drives the run: fresh heartbeat, owner not known to be dead."""
    lease = store.lease(run_id)
    return lease is not None and lease.fresh(ttl_s) and not _dead_on_this_host(lease.owner)


def _dead_on_this_host(owner: str) -> bool:
    pid, _, rest = owner.partition("@")
    host = rest.rpartition(":")[0]
    if host != socket.gethostname() or not pid.isdigit():
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return True
    except PermissionError:  # alive, owned by another user
        return False
    return False


@asynccontextmanager
async def hold_lease(
    store: RunStore,
    run_id: str,
    owner: str,
    *,
    ttl_s: float,
    heartbeat_s: float,
    handed_over: str | None = None,
) -> AsyncIterator[None]:
    """Hold the run's lease for the block, refreshing its heartbeat in the background.

    ``handed_over`` is a token the API took the lease with for this process; it is
    swapped for ``owner`` so the lease names the process that really drives the run.
    Raises ``LeaseHeld`` if another live process drives the run. If the lease is lost
    midway, the block is cancelled and ``LeaseLost`` raised: the new owner drives now.
    A loss noticed while the block was finishing is raised as ``LeaseLost`` too.
    """
    main = asyncio.current_task()
    if main is None:
        raise RuntimeError("hold_lease must run inside an asyncio task")
    if handed_over is None:
        acquire(store, run_id, owner, ttl_s=ttl_s)
    elif not store.take_over_lease(run_id, handed_over, owner):
        raise LeaseLost(f"run {run_id}: the handed-over lease {handed_over} is gone")
    loop = asyncio.get_running_loop()
    heartbeat = _Heartbeat(
        store.path,
        run_id,
        owner,
        ttl_s=ttl_s,
        heartbeat_s=heartbeat_s,
        on_lost=lambda: loop.call_soon_threadsafe(main.cancel),
    )
    heartbeat.start()
    lost = LeaseLost(f"run {run_id}: lease lost to another process")
    try:
        yield
    except asyncio.CancelledError:
        if heartbeat.lost.is_set():
            raise lost from None
        raise
    finally:
        # No loss is reported after this point; a cancel not yet delivered is dropped
        # (the block is over) and the loss is raised below instead.
        pending = heartbeat.request_stop()
        if pending is not None:
            pending.cancel()
        await asyncio.to_thread(heartbeat.join)
        store.release_lease(run_id, owner)
    if heartbeat.lost.is_set():
        raise lost


class _Heartbeat(threading.Thread):
    """Refreshes the lease from its own thread and connection.

    A thread keeps beating while the event loop is busy with synchronous work. A failed
    refresh (e.g. a lock wait timing out) is retried, but only while the next attempt
    would still end before the lease expires; then, or when the lease names someone else,
    the lease is lost and ``on_lost`` stops the driver.
    """

    def __init__(
        self,
        db_path: Path,
        run_id: str,
        owner: str,
        *,
        ttl_s: float,
        heartbeat_s: float,
        on_lost: Callable[[], asyncio.Handle],
    ) -> None:
        super().__init__(name=f"lease-heartbeat-{run_id}", daemon=True)
        self._db_path = db_path
        self._run_id = run_id
        self._owner = owner
        self._heartbeat_s = heartbeat_s
        self._lock_wait_s = min(HEARTBEAT_LOCK_WAIT_S, heartbeat_s)
        # Last moment to keep retrying: one more interval plus a lock wait must fit in the TTL.
        self._give_up_after_s = ttl_s - heartbeat_s - self._lock_wait_s
        self._on_lost = on_lost
        self._guard = threading.Lock()
        self._stopping = threading.Event()
        self._pending: asyncio.Handle | None = None
        self.lost = threading.Event()

    def request_stop(self) -> asyncio.Handle | None:
        """Stop beating; returns the driver's cancel if one was scheduled."""
        with self._guard:
            self._stopping.set()
            return self._pending

    def run(self) -> None:
        busy_ms = int(self._lock_wait_s * 1000)
        store = RunStore.open(self._db_path, busy_timeout_ms=busy_ms)
        try:
            self._beat(store)
        finally:
            store.close()

    def _beat(self, store: RunStore) -> None:
        last_renewed = time.monotonic()
        while not self._stopping.wait(self._heartbeat_s):
            outcome = self._refresh(store)
            if outcome == "renewed":
                last_renewed = time.monotonic()
                continue
            if outcome == "failed" and time.monotonic() - last_renewed < self._give_up_after_s:
                continue
            self._report_lost()
            return

    def _refresh(self, store: RunStore) -> Literal["renewed", "taken_over", "failed"]:
        try:
            return "renewed" if store.renew_lease(self._run_id, self._owner) else "taken_over"
        except Exception:  # noqa: BLE001 - e.g. a lock wait timing out: retried by _beat
            return "failed"

    def _report_lost(self) -> None:
        with self._guard:
            if self._stopping.is_set():
                return
            self.lost.set()
            self._pending = self._on_lost()
