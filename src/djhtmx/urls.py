import asyncio
import logging
import time
from http import HTTPStatus
from typing import cast

from django.apps import apps
from django.core.handlers.asgi import ASGIRequest
from django.core.signing import BadSignature, Signer
from django.db import connections
from django.http.request import HttpRequest, QueryDict
from django.http.response import HttpResponse, StreamingHttpResponse
from django.urls import path, re_path
from django.views.decorators.csrf import csrf_exempt

from .command_response import CommandBatch, to_http_response
from .component import REGISTRY
from .consumer import Consumer
from .introspection import parse_request_data
from .repo import Repository
from .sse_executor import submit_sync_work
from .tracing import htmx_headers_as_tags, sentry_tags, tracing_span

logger = logging.getLogger(__name__)
signer = Signer()


def _non_atomic_for_all_dbs[V](view: V) -> V:
    """Opt an async view out of `ATOMIC_REQUESTS` for *every* configured database.

    Django's `make_view_atomic` raises `RuntimeError("You cannot use
    ATOMIC_REQUESTS with async views.")` for an async view as soon as any
    database has `ATOMIC_REQUESTS` set and the view isn't opted out for that
    alias.  `@transaction.non_atomic_requests` (no args) opts out only the
    default alias, so a non-default `ATOMIC_REQUESTS` DB would still trip it.
    djhtmx applies atomicity per synchronous handler in `_drain_sync_handler`
    (for every atomic alias), so the async views declare themselves non-atomic
    for all databases and leave transaction handling to the dispatcher.
    """
    view._non_atomic_requests = set(connections)  # type: ignore[attr-defined]
    return view


async def endpoint(
    request: HttpRequest, component_name: str, component_id: str, event_handler: str
):
    if "HTTP_HX_SESSION" not in request.META:
        return HttpResponse("Missing header HX-Session", status=HTTPStatus.BAD_REQUEST)

    tags = htmx_headers_as_tags(request.META)

    with sentry_tags(**tags), tracing_span(f"{component_name}.{event_handler}", **tags):
        # Run the whole dispatch as one job on the bounded sync-work pool: it
        # executes on a pool thread that owns a single DB connection, so the
        # process-wide Postgres connection count stays bounded by
        # `DJHTMX_SYNC_WORKERS` rather than scaling with request concurrency.
        return await submit_sync_work(_dispatch_request, request, component_id, event_handler)


def _dispatch_request(request: HttpRequest, component_id: str, event_handler: str) -> HttpResponse:
    """Synchronous HTTP dispatch; runs on a sync-work pool thread.

    `from_request` keeps `request.user` lazy; the pipeline resolves it — and all
    Model fields — here on the pool thread that owns the connection, and the
    template render runs here too.  Nothing in the request path touches the ORM
    on the event loop.
    """
    repo = Repository.from_request(request)
    event_data = parse_request_data(request.POST | request.FILES) | (  # type: ignore[operator]
        {"prompt": prompt}
        if (prompt := request.META.get("HTTP_HX_PROMPT", None)) is not None
        else {}
    )
    batch = CommandBatch.from_processed(
        repo.dispatch_event(component_id, event_handler, event_data)
    )
    return to_http_response(batch)


def _resolve_user(request: HttpRequest):
    """Resolve the request user synchronously (on a sync-work pool thread)."""
    from django.contrib.auth import get_user

    return get_user(request)


@_non_atomic_for_all_dbs
async def sse_endpoint(request: HttpRequest):
    if not isinstance(request, ASGIRequest):
        return HttpResponse("SSE requires ASGI", status=HTTPStatus.NOT_IMPLEMENTED)

    query = cast(QueryDict, request.GET)
    session = query.get("session")
    if not session:
        return HttpResponse("Missing query parameter: session", status=HTTPStatus.BAD_REQUEST)

    try:
        session_id = signer.unsign(session)
    except BadSignature:
        return HttpResponse("Invalid SSE session", status=HTTPStatus.BAD_REQUEST)

    # Resolve the user on the sync-work pool, not on the event loop: a plain
    # `await request.auser()` would acquire a per-async-task DB connection that
    # Django holds for the whole stream lifetime (idle SSE streams would each
    # pin a connection).  Resolving on a pool thread borrows a pooled connection
    # only for the lookup and releases it immediately.
    user = await submit_sync_work(_resolve_user, request)

    async def stream():
        from . import settings
        from .sse import (
            get_async_conn,
            get_sse_heartbeat_paces,
            refresh_sse_session_liveness,
            render_sse_event_fragments,
            render_sse_heartbeat_fragments,
            sse_message,
            wake_channel,
        )
        from .sse_executor import SSERenderQueueFull
        from .utils import compact_hash

        redis = get_async_conn()
        pubsub = redis.pubsub()
        channel = wake_channel(session_id)
        session_tag = compact_hash(session_id)
        logger.debug("SSE [%s] stream subscribe channel=%s", session_id, channel)
        await pubsub.subscribe(channel)

        heartbeat_due_at: dict[int, float] = {}
        refresh_interval = settings.SESSION_REFRESH_INTERVAL
        last_refresh = 0.0
        try:
            logger.debug("SSE [%s] stream connected session", session_id)
            yield b": connected\n\n"
            while True:
                with tracing_span("djhtmx.sse.iteration", session=session_tag):
                    # Keep the Redis keys alive for as long there is a SSE connection
                    now = time.monotonic()
                    if refresh_interval and now - last_refresh >= refresh_interval:
                        await refresh_sse_session_liveness(redis, session_id)
                        last_refresh = now

                    logger.debug("SSE [%s] draining heartbeat subscriptions", session_id)
                    heartbeat_paces = await get_sse_heartbeat_paces(redis, session_id)
                    for pace in heartbeat_paces - heartbeat_due_at.keys():
                        heartbeat_due_at[pace] = now + pace
                    for stale_pace in heartbeat_due_at.keys() - heartbeat_paces:
                        heartbeat_due_at.pop(stale_pace)
                    due_paces = {pace for pace, due_at in heartbeat_due_at.items() if now >= due_at}
                    if due_paces:
                        for pace in due_paces:
                            heartbeat_due_at[pace] = now + pace
                        with tracing_span(
                            "djhtmx.sse.heartbeat_drain",
                            session=session_tag,
                            paces=",".join(str(p) for p in sorted(due_paces)),
                        ):
                            try:
                                heartbeat_fragments = await render_sse_heartbeat_fragments(
                                    redis, session_id, user, due_paces
                                )
                            except SSERenderQueueFull as exc:
                                logger.warning(
                                    "SSE [%s] dropping heartbeat drain: %s", session_id, exc
                                )
                                heartbeat_fragments = []
                        for fragment in heartbeat_fragments:
                            yield sse_message("djhtmx", fragment)

                    logger.debug("SSE [%s] draining session messages", session_id)
                    # This will drain the channel from messages at both connection time and later
                    # after a message is received (reentering the loop).
                    #
                    # Caveat: if the Redis pub/sub connection disconnects or the worker is
                    # restarted during that interval, the pub/sub wake can be lost.  That is why
                    # the loop drains pending events at the top before sleeping.  In that failure
                    # case, the event remains queued, but without another wake it might wait until
                    # the next heartbeat timeout or another publish causes the loop to check
                    # again.  The wait is capped by `SSE_HEARTBEAT_TIMEOUT`, so worst-case delay
                    # is roughly that value.
                    with tracing_span("djhtmx.sse.event_drain", session=session_tag):
                        try:
                            event_fragments = await render_sse_event_fragments(session_id, user)
                        except SSERenderQueueFull as exc:
                            logger.warning("SSE [%s] dropping event drain: %s", session_id, exc)
                            event_fragments = []
                    for fragment in event_fragments:
                        yield sse_message("djhtmx", fragment)

                    logger.debug(
                        "SSE [%s] waiting for wake up call on channel '%s'", session_id, channel
                    )
                    timeout = settings.SSE_HEARTBEAT_TIMEOUT
                    if heartbeat_due_at:
                        next_heartbeat_tick = min(heartbeat_due_at.values())
                        timeout = max(0, min(timeout, next_heartbeat_tick - time.monotonic()))
                with tracing_span("djhtmx.sse.wait", session=session_tag, timeout=f"{timeout:.2f}"):
                    message = await pubsub.get_message(
                        ignore_subscribe_messages=True, timeout=timeout
                    )
                if not message:
                    yield b": heartbeat\n\n"
        except asyncio.CancelledError:
            logger.info("SSE [%s] stream cancelled", session_id)
            raise
        except Exception:
            logger.exception("SSE [%s] stream error", session_id)
            raise
        finally:
            logger.debug("SSE [%s] stream closing channel=%s", session_id, channel)
            await pubsub.unsubscribe(channel)
            await pubsub.close()

    return StreamingHttpResponse(
        stream(),
        content_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


APP_CONFIGS = sorted(apps.app_configs.values(), key=lambda app_config: -len(app_config.name))


def app_name_of_component(cls: type):
    cls_module = cls.__module__
    for app_config in APP_CONFIGS:
        if cls_module.startswith(app_config.name):
            return app_config.label
    return cls_module


def _make_endpoint_view(component_name: str):
    """Build the per-component HTTP event view, opted out of ATOMIC_REQUESTS.

    Django's `make_view_atomic` raises `RuntimeError("You cannot use
    ATOMIC_REQUESTS with async views.")` for any async view while a database has
    `ATOMIC_REQUESTS` set, so it would reject this endpoint before it ever runs.
    djhtmx instead applies atomicity per synchronous handler in
    `_drain_sync_handler`; the endpoint therefore declares itself non-atomic so
    Django leaves the transaction handling to the dispatcher.
    """

    @csrf_exempt
    @_non_atomic_for_all_dbs
    async def view(request: HttpRequest, component_id: str, event_handler: str):
        return await endpoint(
            request,
            component_name=component_name,
            component_id=component_id,
            event_handler=event_handler,
        )

    return view


urlpatterns = [
    path("_sse/connect", sse_endpoint, name="djhtmx.sse"),
    *[
        path(
            f"{app_name_of_component(component)}/{component_name}/<component_id>/<event_handler>",
            _make_endpoint_view(component_name),
            name=f"djhtmx.{component_name}",
        )
        for component_name, component in REGISTRY.items()
    ],
]


ws_urlpatterns = [
    re_path("ws", Consumer.as_asgi(), name="djhtmx.ws"),  # type: ignore
]

__all__ = (
    "urlpatterns",
    "ws_urlpatterns",
)
