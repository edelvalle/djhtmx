from itertools import chain
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

        component_id = request.META["HTTP_HX_TARGET"]
        states_by_id = {
            state["id"]: state
            for state in [
                json.loads(signer.unsign(state))
                for state in request.POST.getlist("__hx-states__")
            ]
        }

        subscriptions_by_id = {
            component_id: subscriptions.split(",")
            for component_id, subscriptions in json.loads(
                request.POST["__hx-subscriptions__"]
            ).items()
        }

        repo = Repository(request, states_by_id, subscriptions_by_id)
        component = repo.build(component_name, states_by_id[component_id])
        handler = getattr(component, event_handler)
        handler_kwargs = parse_request_data(request.POST)
        handler_kwargs = filter_parameters(handler, handler_kwargs)

        response = handler(**handler_kwargs) or repo.render(component)

        for oob_render in chain.from_iterable(
            [repo.dispatch_signals(), repo.render_oob()]
        ):
            response._container.append(b"\n")  # type: ignore
            response._container.append(response.make_bytes(oob_render))  # type: ignore

        if params != repo.params:
            response["HX-Push-Url"] = (
                "?" + component.controller.params.urlencode()
            )

        return response


urlpatterns = [
    path(
        "<component_name>/<event_handler>",
        endpoint,
        name="djhtmx.endpoint",
    )
]
