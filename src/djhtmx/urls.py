from functools import partial

from django.urls import path

from . import json
from .component import REGISTRY, Component
from .executor import Executor, signer
from .introspection import filter_parameters, parse_request_data
from .tracing import sentry_request_transaction


def endpoint(request, component_name, component_id, event_handler):
    with sentry_request_transaction(request, component_name, event_handler):
        executor = Executor(
            request,
            component_name,
            component_id,
            event_handler,
        )
        return executor()


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
        partial(endpoint, component_name=component_name),
        name=f"djhtmx.{component_name}",
    )
    for component_name in REGISTRY
]

urlpatterns += [
    path(
        "<component_name>/<component_id>/<event_handler>",
        legacy_endpoint,
        name="djhtmx.legacy_endpoint",
    )
]
