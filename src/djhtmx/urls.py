from functools import partial

from django.core.signing import Signer
from django.urls import path, re_path

from . import json
from .component import Component
from .consumer import Consumer
from .introspection import filter_parameters, parse_request_data
from .tracing import sentry_request_transaction

signer = Signer()


def legacy_endpoint(request, component_name, component_id, event_handler):
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


urlpatterns = [
    path(
        f"{component_name}/<component_id>/<event_handler>",
        partial(legacy_endpoint, component_name=component_name),
        name=f"djhtmx.{component_name}",
    )
    for component_name in Component._all
]


ws_urlpatterns = [
    re_path("ws", Consumer.as_asgi(), name="djhtmx.ws"),  # type: ignore
]
