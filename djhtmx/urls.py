from django.core.signing import Signer
from django.urls import path

from .component import Component
from .introspection import extract_data


def endpoint(request, component_name, event_handler):
    ComponentClass = Component._all[component_name]
    id = request.META.get('HTTP_HX_TARGET')

    state = request.META.get('HTTP_X_COMPONENT_STATE', '')
    state = Signer().unsign(state)
    state = dict(ComponentClass._constructor_model.parse_raw(state), id=id)
    component = ComponentClass(request=request, **state)

    handler_kwargs = extract_data(request)
    handler_kwargs = component._models[event_handler](**handler_kwargs).dict()
    return (
        getattr(component, event_handler)(**handler_kwargs) or
        component.render()
    )


urlpatterns = [
    path(
        '<component_name>/<event_handler>',
        endpoint,
        name='djhtmx.endpoint',
    )
]
