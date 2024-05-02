from itertools import chain

from django.core.signing import Signer
from django.urls import path

from . import json
from .component import REGISTRY, Component, Repository, get_params
from .introspection import filter_parameters, parse_request_data
from .tracing import sentry_request_transaction

signer = Signer()


def endpoint(request, component_name, component_id, event_handler):
    with sentry_request_transaction(request, component_name, event_handler):
        if component_name in REGISTRY:
            # PydanticComponent
            params = get_params(request)

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
                    request.POST.get("__hx-subscriptions__", "{}")
                ).items()
            }

            repo = Repository.from_request(
                request,
                states_by_id,
                subscriptions_by_id,
            )
            component = repo.build(component_name, states_by_id[component_id])
            handler = getattr(component, event_handler)
            handler_kwargs = parse_request_data(request.POST)
            handler_kwargs = filter_parameters(handler, handler_kwargs)

            template = handler(**handler_kwargs)
            if isinstance(template, tuple):
                target, template = template
            else:
                target = None
            response = repo.render(component, template=template)

            if isinstance(template, str):
                # if there was a partial response, send the state for update
                response["HX-State"] = json.dumps(
                    {
                        "component_id": component.id,
                        "state": signer.sign(component.model_dump_json()),
                    }
                )
                if isinstance(target, str):
                    response["HX-Retarget"] = target

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
        else:
            # Legacy Component

            state = request.META.get("HTTP_X_COMPONENT_STATE", "")
            state = Signer().unsign(state)
            state = json.loads(state)
            component = Component._build(
                component_name, request, component_id, state
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
