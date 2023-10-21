from django.core.signing import Signer
from django.urls import path

from . import json
from .component import Repository, get_params
from .introspection import filter_parameters, parse_request_data
from .tracing import sentry_request_transaction

signer = Signer()


def endpoint(request, component_name, event_handler):
    with sentry_request_transaction(request, component_name, event_handler):
        params = get_params(request)

        component_id = request.META['HTTP_HX_TARGET']
        states_by_id = {
            state["id"]: state
            for state in [
                json.loads(signer.unsign(state))
                for state in request.POST.getlist("__hx-states__")
            ]
        }

        repo = Repository(request, states_by_id)
        component = repo.build(component_name, states_by_id[component_id])
        handler = getattr(component, event_handler)
        handler_kwargs = parse_request_data(request.POST)
        handler_kwargs = filter_parameters(handler, handler_kwargs)
        handler(**handler_kwargs)

        response = repo.render(component)

        if params != repo.params:
            response["HX-Push-Url"] = (
                '?' + component.controller.params.urlencode()
            )

        return response


urlpatterns = [
    path(
        '<component_name>/<event_handler>',
        endpoint,
        name='djhtmx.endpoint',
    )
]
