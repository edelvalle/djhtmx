from django.core.signing import Signer
from django.urls import path

from . import json
from .component import Component
from .introspection import extract_data


def endpoint(request, component_name, id, event_handler):
    state = request.META.get('HTTP_X_COMPONENT_STATE', '')
    state = Signer().unsign(state)
    state = json.loads(state)
    component = Component._build(component_name, request, id, state)

    handler_kwargs = extract_data(request)
    handler_kwargs = component._models[event_handler](**handler_kwargs).dict()
    return (
        getattr(component, event_handler)(**handler_kwargs) or
        component.render()
    )


urlpatterns = [
    path(
        '<component_name>/<id>/<event_handler>',
        endpoint,
        name='djhtmx.endpoint',
    )
]
