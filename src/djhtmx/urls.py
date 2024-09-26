from functools import partial
from itertools import chain

from django.core.signing import Signer
from django.http.request import HttpRequest
from django.urls import path, re_path

from . import json
from .component import Component
from .consumer import Consumer
from .introspection import filter_parameters, parse_request_data
from .tracing import sentry_request_transaction

signer = Signer()


# def endpoint(request: HttpRequest, component_name: str, component_id: str, event_handler: str):
#     repo = Repository.from_request(request)


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
        # (
        #     path(
        #         f"{component_name}/<component_id>/<event_handler>",
        #         partial(endpoint, component_name=component_name),
        #         name=f"djhtmx.{component_name}",
        #     )
        #     for component_name in REGISTRY
        # ),
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
