import json

from django.core.signing import Signer
from django.urls import path

from .component import HTMXComponent
from .introspection import filter_parameters, parse_request_data
from .tracing import sentry_request_transaction


def endpoint(request, component_name, event_handler):
    with sentry_request_transaction(request, component_name, event_handler):
        state = request.META.get('HTTP_X_COMPONENT_STATE', '')
        state = Signer().unsign(state)
        state = json.loads(state)
        component = HTMXComponent._build(component_name, request, state)
        handler = getattr(component, event_handler)
        handler_kwargs = parse_request_data(getattr(request, request.method))
        handler_kwargs = filter_parameters(handler, handler_kwargs)
        return handler(**handler_kwargs) or component.render()


urlpatterns = [
    path(
        '<component_name>/<event_handler>',
        endpoint,
        name='djhtmx.endpoint',
    )
]
