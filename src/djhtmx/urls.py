import asyncio
import logging
import time
from functools import partial
from http import HTTPStatus
from typing import assert_never, cast

from django.apps import apps
from django.core.handlers.asgi import ASGIRequest
from django.core.signing import BadSignature, Signer
from django.db import transaction
from django.http.request import HttpRequest, QueryDict
from django.http.response import HttpResponse, StreamingHttpResponse
from django.urls import path, re_path
from django.utils.html import format_html
from django.views.decorators.csrf import csrf_exempt

from .commands import PushURL, ReplaceURL, SendHtml
from .component import (
    REGISTRY,
    Destroy,
    DispatchDOMEvent,
    Focus,
    Open,
    Redirect,
    ScrollIntoView,
    Triggers,
)
from .consumer import Consumer
from .introspection import parse_request_data
from .repo import Repository
from .tracing import htmx_headers_as_tags, sentry_tags, tracing_span

logger = logging.getLogger(__name__)
signer = Signer()


def endpoint(request: HttpRequest, component_name: str, component_id: str, event_handler: str):
    if "HTTP_HX_SESSION" not in request.META:
        return HttpResponse("Missing header HX-Session", status=HTTPStatus.BAD_REQUEST)

    tags = htmx_headers_as_tags(request.META)

    with sentry_tags(**tags), tracing_span(f"{component_name}.{event_handler}", **tags):
        repo = Repository.from_request(request)
        content: list[str] = []
        headers: dict[str, str] = {}
        triggers = Triggers()

        for command in repo.dispatch_event(
            component_id,
            event_handler,
            parse_request_data(request.POST | request.FILES)  # type: ignore[reportOperatorIssues]
            | (
                {"prompt": prompt}
                if (prompt := request.META.get("HTTP_HX_PROMPT", None)) is not None
                else {}
            ),
        ):
            # Command loop
            match command:
                case Destroy(component_id):
                    content.append(
                        format_html(
                            '<div id="{component_id}" hx-swap-oob="delete"></div>',
                            component_id=component_id,
                        )
                    )
                case Redirect(url):
                    headers["HX-Redirect"] = url
                case Focus(selector):
                    triggers.after_settle("hxFocus", selector)
                case ScrollIntoView(selector, behavior, block, if_not_visible):
                    triggers.after_settle(
                        "hxScrollIntoView",
                        {
                            "selector": selector,
                            "behavior": behavior,
                            "block": block,
                            "if_not_visible": if_not_visible,
                        },
                    )
                case Open(url, name, target, rel):
                    triggers.after_settle(
                        "hxOpenURL",
                        {"url": url, "name": name, "target": target, "rel": rel},
                    )
                case DispatchDOMEvent(target, event, detail, bubbles, cancelable, composed):
                    triggers.after_settle(
                        "hxDispatchDOMEvent",
                        {
                            "event": event,
                            "target": target,
                            "detail": detail,
                            "bubbles": bubbles,
                            "cancelable": cancelable,
                            "composed": composed,
                        },
                    )
                case SendHtml(html):
                    content.append(html)
                case PushURL(url):
                    headers["HX-Push-Url"] = url
                case ReplaceURL(url):
                    headers["HX-Replace-Url"] = url
                case _ as unreachable:
                    assert_never(unreachable)

        # HX-Redirect triggers a full navigation, so URL manipulation headers
        # are meaningless and can cause HTMX to skip the redirect.
        if "HX-Redirect" in headers:
            headers.pop("HX-Replace-Url", None)
            headers.pop("HX-Push-Url", None)

        return HttpResponse("\n\n".join(content), headers=headers | triggers.headers)


@transaction.non_atomic_requests
async def sse_endpoint(request: HttpRequest):
    if not isinstance(request, ASGIRequest):
        return HttpResponse("SSE requires ASGI", status=HTTPStatus.NOT_IMPLEMENTED)

    user = getattr(request, "user", None)
    query = cast(QueryDict, request.GET)
    session = query.get("session")
    if not session:
        return HttpResponse("Missing query parameter: session", status=HTTPStatus.BAD_REQUEST)

    try:
        session_id = signer.unsign(session)
    except BadSignature:
        return HttpResponse("Invalid SSE session", status=HTTPStatus.BAD_REQUEST)

    await asyncio.sleep(0)

    async def stream():
        from . import settings
        from .sse import (
            get_async_conn,
            refresh_sse_session_liveness,
            render_sse_event_fragments,
            sse_message,
            wake_channel,
        )

        redis = get_async_conn()
        pubsub = redis.pubsub()
        channel = wake_channel(session_id)
        logger.debug("SSE [%s] stream subscribe channel=%s", session_id, channel)
        await pubsub.subscribe(channel)
        last_refresh = 0.0
        try:
            logger.debug("SSE [%s] stream connected session", session_id)
            yield b": connected\n\n"
            while True:
                now = time.monotonic()
                if (
                    settings.SESSION_REFRESH_INTERVAL
                    and now - last_refresh >= settings.SESSION_REFRESH_INTERVAL
                ):
                    await refresh_sse_session_liveness(redis, session_id)
                    last_refresh = now

                logger.debug("SSE [%s] draining session messages", session_id)
                # This will drain the channel from messages at both connection time and later after
                # a message is received (reentering the loop).
                #
                # Caveat: if the Redis pub/sub connection disconnects or the worker is restarted
                # during that interval, the pub/sub wake can be lost.  That is why the loop drains
                # pending events at the top before sleeping.  In that failure case, the event
                # remains queued, but without another wake it might wait until the next heartbeat
                # timeout or another publish causes the loop to check again.  Current timeout is
                # 15s, so worst-case delay is roughly heartbeat interval.
                for fragment in await render_sse_event_fragments(session_id, user):
                    yield sse_message("djhtmx", fragment)
                logger.debug(
                    "SSE [%s] waiting for wake up call on channel '%s'", session_id, channel
                )
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=15)
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


urlpatterns = [
    path("_sse/connect", sse_endpoint, name="djhtmx.sse"),
    *[
        path(
            f"{app_name_of_component(component)}/{component_name}/<component_id>/<event_handler>",
            csrf_exempt(partial(endpoint, component_name=component_name)),
            name=f"djhtmx.{component_name}",
        )
        for component_name, component in REGISTRY.items()
    ],
]


ws_urlpatterns = [
    re_path("ws", Consumer.as_asgi(), name="djhtmx.ws"),  # type: ignore
]
