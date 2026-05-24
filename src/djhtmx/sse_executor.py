"""SSE render executor.

Dedicated thread pool that hosts the sync render path used by the SSE
endpoint.  Each worker thread keeps its own Django DB connection across
renders, so PG connection count is bounded by `SSE_RENDER_WORKERS`
rather than growing with SSE stream count.

The submitted callable is opaque to the executor: connection lifecycle
(periodic health check, periodic rotation, close on `OperationalError`/
`InterfaceError`) is managed here, not in the callable.
"""

from __future__ import annotations

import asyncio
import atexit
import contextvars
import functools
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from asgiref.sync import sync_to_async
from django.db import InterfaceError, OperationalError, connections

from . import settings
from .utils.runtime import is_testing

logger = logging.getLogger(__name__)

_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()

# Per-worker-thread render counter.  Used to decide when to run a health
# check and when to rotate the DB connection.  Keyed by thread ident.
_render_counts: dict[int, int] = {}
_render_counts_lock = threading.Lock()


def get_sse_render_executor() -> ThreadPoolExecutor:
    """Return the lazily-constructed SSE render executor.

    Constructed on first call and reused for the lifetime of the process.
    The corresponding `atexit` shutdown is registered the first time the
    executor comes up.
    """
    global _executor
    if _executor is None:
        with _executor_lock:
            if _executor is None:
                _executor = ThreadPoolExecutor(
                    max_workers=settings.SSE_RENDER_WORKERS,
                    thread_name_prefix="djhtmx-sse-render",
                )
                atexit.register(_shutdown_executor)
                logger.info(
                    "djhtmx SSE render executor started with %d workers",
                    settings.SSE_RENDER_WORKERS,
                )
    return _executor


def _shutdown_executor() -> None:
    global _executor
    if _executor is not None:
        logger.info("djhtmx SSE render executor shutting down")
        _executor.shutdown(wait=True)
        _executor = None


async def submit_sse_render[**P, R](
    fn: Callable[P, R],
    /,
    *args: P.args,
    **kwargs: P.kwargs,
) -> R:
    """Run `fn(*args, **kwargs)` on the SSE render executor.

    Propagates the current `contextvars.Context` to the worker thread so
    Sentry scope, tracing spans, and djhtmx context-locals remain
    available inside the render.

    Raises `SSERenderQueueFull` if `SSE_RENDER_QUEUE_MAX` is set and the
    executor's pending queue is at cap.
    """
    if is_testing():
        # `TestCase`-style transactional tests need the render to land back
        # on the test thread so the uncommitted test transaction is visible
        # via the shared DB connection.  Thread-sensitive `sync_to_async`
        # pairs with the `async_to_sync` entry point in `testing.Htmx` to
        # route the call back to the test thread.
        return await sync_to_async(fn, thread_sensitive=True)(*args, **kwargs)

    executor = get_sse_render_executor()
    if settings.SSE_RENDER_QUEUE_MAX and _approx_pending(executor) >= settings.SSE_RENDER_QUEUE_MAX:
        raise SSERenderQueueFull(f"SSE render queue at cap ({settings.SSE_RENDER_QUEUE_MAX})")

    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    bound = functools.partial(fn, *args, **kwargs)
    return await loop.run_in_executor(executor, _run_with_lifecycle, ctx, bound)


class SSERenderQueueFull(RuntimeError):
    """Raised when the SSE render executor's pending queue is at cap."""


def _approx_pending(executor: ThreadPoolExecutor) -> int:
    queue = getattr(executor, "_work_queue", None)
    return queue.qsize() if queue is not None else 0


def _run_with_lifecycle[R](ctx: contextvars.Context, bound: Callable[[], R]) -> R:
    return ctx.run(_render_with_connection_lifecycle, bound)


def _render_with_connection_lifecycle[R](bound: Callable[[], R]) -> R:
    thread_id = threading.get_ident()
    count = _bump_render_count(thread_id)

    if settings.SSE_RENDER_HEALTHCHECK_EVERY and count % settings.SSE_RENDER_HEALTHCHECK_EVERY == 0:
        _health_check_connections()

    try:
        return bound()
    except (OperationalError, InterfaceError):
        logger.warning("djhtmx SSE render: closing broken DB connection on worker", exc_info=True)
        _close_connections()
        raise
    finally:
        if settings.SSE_RENDER_ROTATE_EVERY and count % settings.SSE_RENDER_ROTATE_EVERY == 0:
            _close_connections()


def _bump_render_count(thread_id: int) -> int:
    with _render_counts_lock:
        count = _render_counts.get(thread_id, 0) + 1
        _render_counts[thread_id] = count
        return count


def _health_check_connections() -> None:
    for conn in connections.all():
        if conn.connection is None:
            continue
        try:
            if not conn.is_usable():
                conn.close()
        except Exception:
            logger.warning("djhtmx SSE render: health check failed; closing", exc_info=True)
            conn.close()


def _close_connections() -> None:
    for conn in connections.all():
        conn.close()


def reset_for_tests() -> None:
    """Tear down the executor and per-thread counters.  Tests only."""
    global _executor
    with _executor_lock:
        if _executor is not None:
            _executor.shutdown(wait=True)
            _executor = None
    with _render_counts_lock:
        _render_counts.clear()


def _executor_for_introspection() -> ThreadPoolExecutor | None:
    """Return the executor without constructing one.  For tests/metrics."""
    return _executor


__all__ = [
    "SSERenderQueueFull",
    "get_sse_render_executor",
    "reset_for_tests",
    "submit_sse_render",
]
