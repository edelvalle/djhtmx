from django.urls import path

from . import json
from .component import REGISTRY, Component
from .executor import Executor, signer
from .introspection import filter_parameters, parse_request_data
from .tracing import sentry_request_transaction


def endpoint(request, component_name, component_id, event_handler):
    with sentry_request_transaction(request, component_name, event_handler):
        if component_name in REGISTRY:
            # PydanticComponent
            executor = Executor(
                request,
                component_name,
                component_id,
                event_handler,
            )
            return executor()
        else:
            # Legacy Component
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
        "<component_name>/<component_id>/<event_handler>",
        endpoint,
        name="djhtmx.endpoint",
    )
]
