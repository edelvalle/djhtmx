from functools import partial
from http import HTTPStatus
from itertools import chain
from typing import assert_never

from django.core.signing import Signer
from django.http.request import HttpRequest
from django.http.response import HttpResponse
from django.urls import path, re_path
from django.utils.html import format_html

from . import json
from .component import (
    REGISTRY,
    Component,
    Destroy,
    DispatchDOMEvent,
    Focus,
    Open,
    Redirect,
    Triggers,
)
from .consumer import Consumer
from .introspection import filter_parameters, parse_request_data
from .repo import PushURL, Repository, SendHtml
from .tracing import sentry_request_transaction

signer = Signer()


def endpoint(request: HttpRequest, component_name: str, component_id: str, event_handler: str):
    if "HTTP_HX_SESSION" not in request.META:
        return HttpResponse("Missing header HX-Session", status=HTTPStatus.BAD_REQUEST)

    with sentry_request_transaction(request, component_name, event_handler):
        repo = Repository.from_request(request)
        content: list[str] = []
        headers: dict[str, str] = {}
        triggers = Triggers()

        for command in repo.dispatch_event(
            component_id,
            event_handler,
            parse_request_data(request.POST),
        ):
            # Command loop
            match command:
                case Destroy(component_id):
                    content.append(
                        format_html(
                            '<div hx-swap-oob="outerHtml:#{component_id}"></div>',
                            component_id=component_id,
                        )
                    )
                case Redirect(url):
                    headers["HX-Redirect"] = url
                case Focus(selector):
                    triggers.after_settle("hxFocus", selector)
                case Open(url, name, target, rel):
                    triggers.after_settle(
                        "hxOpenURL",
                        {"url": url, "name": name, "target": target, "rel": rel},
                    )
                case DispatchDOMEvent(event, target, detail, bubbles, cancelable, composed):
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
                case _ as unreachable:
                    assert_never(unreachable)

        return HttpResponse("\n\n".join(content), headers=headers | triggers.headers)


def legacy_endpoint(
    request: HttpRequest, component_name: str, component_id: str, event_handler: str
):
    with sentry_request_transaction(request, component_name, event_handler):
        state = request.META.get("HTTP_X_COMPONENT_STATE", "{}")
        state = signer.unsign(state)
        state = json.loads(state)
        component = Component._build(
            component_name,
            request,
            component_id,
            state,
        )
        handler = getattr(component, event_handler)
        handler_kwargs = parse_request_data(request.POST)
        handler_kwargs = filter_parameters(handler, handler_kwargs)
        return handler(**handler_kwargs) or component.render()


urlpatterns = list(
    chain(
        (
            path(
                f"{component_name}/<component_id>/<event_handler>",
                partial(endpoint, component_name=component_name),
                name=f"djhtmx.{component_name}",
            )
            for component_name in REGISTRY
        ),
        (
            path(
                f"{component_name}/<component_id>/<event_handler>",
                partial(legacy_endpoint, component_name=component_name),
                name=f"djhtmx.{component_name}",
            )
            for component_name in Component._all
        ),
    )
)


ws_urlpatterns = [
    re_path("ws", Consumer.as_asgi(), name="djhtmx.ws"),  # type: ignore
]
