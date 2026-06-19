#!/usr/bin/env python
"""Async concurrency / connection-bound load test for the djhtmx async pipeline.

What it validates
-----------------
1. **No deadlock under concurrent drains.**  It fires many concurrent SSE
   drains.  Each drain runs as a single synchronous job on the sync-work pool;
   the job never re-submits to the pool, so concurrent drains beyond
   ``DJHTMX_SYNC_WORKERS`` simply queue and run as threads free up.  A
   regression shows up as the run hanging until the timeout.

2. **DB-connection bound.**  The whole drain (build + handlers + render) runs on
   one pool thread that owns one Django DB connection, so the pool never grows
   past ``DJHTMX_SYNC_WORKERS`` worker threads regardless of how many drains are
   in flight — and an idle SSE stream holds no connection at all.

Run
---
    # set a deliberately small pool to make the bound (and any regression) obvious
    DJHTMX_SYNC_WORKERS=4 python src/tests/loadtest_async.py [N_DRAINS]

Requires a running Redis (``DJHTMX_REDIS_URL``).  Uses the fision sample app and
its sqlite DB.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from uuid import uuid4

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "fision.settings")

import django

django.setup()

from asgiref.sync import sync_to_async  # noqa: E402
from django.contrib.auth.models import AnonymousUser  # noqa: E402
from django.core.management import call_command  # noqa: E402
from fision.todo.htmx import TODO_ITEMS_TOPIC, TodoCounter, TodoItemAdded  # noqa: E402

from djhtmx import settings as djsettings  # noqa: E402
from djhtmx.repo import Repository, Session  # noqa: E402
from djhtmx.sse import (  # noqa: E402
    aemit_sse_event,
    register_component,
    render_sse_event_fragments,
)
from djhtmx.sse_executor import get_sse_render_executor, reset_for_tests  # noqa: E402

N_DRAINS = int(sys.argv[1]) if len(sys.argv) > 1 else 16
# `django.setup()` imports djhtmx.settings, freezing the pool size, so override
# the module constants here (before the executor is lazily built on first use).
# The executor reads `SSE_RENDER_WORKERS` (alias of `SYNC_WORKERS`).
WORKERS = int(sys.argv[2]) if len(sys.argv) > 2 else int(os.environ.get("DJHTMX_SYNC_WORKERS", 4))
djsettings.SYNC_WORKERS = WORKERS
djsettings.SSE_RENDER_WORKERS = WORKERS
TIMEOUT = 120
USER = AnonymousUser()


async def setup_session() -> str:
    """Create a session with a registered TodoCounter consumer and one queued
    SSE event, mirroring what a live SSE client would have pending."""
    sid = Repository.new_session_id()
    session = Session(sid)
    session.read = True  # fresh session; nothing to read back
    counter = TodoCounter(hx_name="TodoCounter", id="counter", user=None, session_id=sid)
    session.store(counter)
    await sync_to_async(session.flush)()
    await sync_to_async(register_component)(sid, counter)
    # Enqueue an event from a *different* source session so it is delivered.
    await aemit_sse_event(
        TodoItemAdded(item_id=uuid4()),
        topics={TODO_ITEMS_TOPIC},
        source_session_id="loadtest:other",
    )
    return sid


async def drain(sid: str) -> tuple[float, int]:
    t0 = time.monotonic()
    fragments = await render_sse_event_fragments(sid, USER)
    return time.monotonic() - t0, len(fragments)


class PoolThreadSampler:
    """Background sampler of how many sync-work pool threads are alive."""

    def __init__(self) -> None:
        self.peak = 0
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _count(self) -> int:
        return sum(1 for t in threading.enumerate() if t.name.startswith("djhtmx-sse-render"))

    def _run(self) -> None:
        while not self._stop.is_set():
            self.peak = max(self.peak, self._count())
            time.sleep(0.005)

    def __enter__(self) -> PoolThreadSampler:
        self._t.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._t.join()


async def main() -> int:
    print(f"djhtmx async load test: N_DRAINS={N_DRAINS} DJHTMX_SYNC_WORKERS={WORKERS}")
    reset_for_tests()  # ensure the executor is (re)built at the overridden size
    await sync_to_async(call_command)("migrate", verbosity=0)

    print(f"setting up {N_DRAINS} sessions (register consumer + queue 1 event each)…")
    sids = await asyncio.gather(*[setup_session() for _ in range(N_DRAINS)])

    print(f"firing {N_DRAINS} concurrent SSE drains (timeout {TIMEOUT}s)…")
    with PoolThreadSampler() as sampler:
        t0 = time.monotonic()
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*[drain(sid) for sid in sids], return_exceptions=True),
                timeout=TIMEOUT,
            )
        except TimeoutError:
            print(f"\n  ✗ DEADLOCK/HANG: drains did not finish within {TIMEOUT}s")
            return 1
        elapsed = time.monotonic() - t0

    ok = [r for r in results if not isinstance(r, BaseException)]
    errs = [r for r in results if isinstance(r, BaseException)]
    durations = sorted(d for d, _ in ok)

    total_fragments = sum(n for _, n in ok)
    print(f"\n  completed {len(ok)}/{N_DRAINS} drains in {elapsed:.2f}s")
    print(f"  total rendered SSE fragments: {total_fragments} (each drain hits the pool to render)")
    if durations:
        print(
            f"  per-drain: min={durations[0]:.2f}s "
            f"median={durations[len(durations) // 2]:.2f}s max={durations[-1]:.2f}s"
        )
    print(f"  configured pool size (DJHTMX_SYNC_WORKERS): {WORKERS}")
    print(f"  executor max_workers: {get_sse_render_executor()._max_workers}")
    print(f"  peak live pool threads observed: {sampler.peak}")
    if errs:
        print(f"  {len(errs)} drains raised (first: {type(errs[0]).__name__}: {errs[0]})")

    bound_ok = sampler.peak <= WORKERS
    completed = len(ok) + len(errs) == N_DRAINS
    print()
    print(f"  [{'PASS' if completed else 'FAIL'}] no deadlock — all drains returned")
    print(f"  [{'PASS' if bound_ok else 'FAIL'}] connection bound — pool threads ≤ {WORKERS}")
    return 0 if (completed and bound_ok) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
