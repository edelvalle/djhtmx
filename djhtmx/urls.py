from django.core.signing import Signer
from django.urls import path

from .component import Component
from .introspection import parse_request_data, filter_parameters
from . import json


def endpoint(request, component_name, event_handler):
    id = request.META.get('HTTP_HX_TARGET')
    state = request.META.get('HTTP_X_COMPONENT_STATE', '')
    state = Signer().unsign(state)
    state = json.loads(state)
    component = Component._build(component_name, request, id, state)
    handler = getattr(component, event_handler)
    handler_kwargs = parse_request_data(request)
    handler_kwargs = filter_parameters(handler, handler_kwargs)
    return handler(**handler_kwargs) or component.render()


urlpatterns = [
    path(
        '<component_name>/<event_handler>',
        endpoint,
        name='djhtmx.endpoint',
    )
]
