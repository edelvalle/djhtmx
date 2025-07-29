from functools import partial
from http import HTTPStatus
from typing import assert_never

from django.apps import apps
from django.core.signing import Signer
from django.http.request import HttpRequest
from django.http.response import HttpResponse
from django.urls import path, re_path
from django.utils.html import format_html

from .commands import PushURL, ReplaceURL, SendHtml
from .component import REGISTRY, Destroy, DispatchDOMEvent, Focus, Open, Redirect, Triggers
from .consumer import Consumer
from .introspection import parse_request_data
from .repo import Repository
from .tracing import tracing_span

signer = Signer()


def endpoint(request: HttpRequest, component_name: str, component_id: str, event_handler: str):
    if "HTTP_HX_SESSION" not in request.META:
        return HttpResponse("Missing header HX-Session", status=HTTPStatus.BAD_REQUEST)

    with tracing_span(f"{component_name}.{event_handler}"):
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

        return HttpResponse("\n\n".join(content), headers=headers | triggers.headers)


APP_CONFIGS = sorted(apps.app_configs.values(), key=lambda app_config: -len(app_config.name))


def app_name_of_component(cls: type):
    cls_module = cls.__module__
    for app_config in APP_CONFIGS:
        if cls_module.startswith(app_config.name):
            return app_config.label
    return cls_module


urlpatterns = [
    path(
        f"{app_name_of_component(component)}/{component_name}/<component_id>/<event_handler>",
        partial(endpoint, component_name=component_name),
        name=f"djhtmx.{component_name}",
    )
    for component_name, component in REGISTRY.items()
]


ws_urlpatterns = [
    re_path("ws", Consumer.as_asgi(), name="djhtmx.ws"),  # type: ignore
]
