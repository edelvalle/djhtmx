from contextlib import ExitStack
from dataclasses import dataclass
from itertools import chain

from django.http import Http404
from django.http.response import HttpResponse

from . import json
from .component import QS_MAP, Repository, RequestWithRepo, get_params, signer
from .introspection import filter_parameters, parse_request_data


@dataclass(slots=True)
class Executor:
    request: RequestWithRepo
    component_name: str
    component_id: str
    event_handler: str

    def __call__(self):
        params = get_params(self.request)
        repo = Repository.from_request(self.request)

        component = repo.get_component_by_id(self.component_id)
        if not component:
            raise Http404

        handler = getattr(component, self.event_handler)
        handler_kwargs = parse_request_data(self.request.POST)
        handler_kwargs = filter_parameters(handler, handler_kwargs)

        template = None
        with ExitStack() as stack:
            for patcher in QS_MAP.get(self.component_name, []):
                stack.enter_context(
                    patcher.tracking_query_string(repo, component)
                )
            template = handler(**handler_kwargs)

        if isinstance(template, tuple):
            target, template = template
        else:
            target = None

        # Before trying to render the component, we must dispatch all the
        # signals/events because that could potentially render the main
        # component.
        partials = {
            component_id: result
            for component_id, result in repo.dispatch_signals(
                main_component_id=component.id
            )
        }

        # If the partials don't contain the main component, we must render it.
        # Otherwise,
        if main_partial := partials.pop(component.id, None):
            controller = component.controller
            response = controller._apply_headers(HttpResponse(main_partial))
        else:
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

        # Now we append all the rendered partials and the custom oob added by
        # the components.
        for oob_render in chain(
            partials.values(),
            repo.render_oob(),
            repo.render_assets(),
        ):
            response._container.append(b"\n")  # type: ignore
            response._container.append(response.make_bytes(oob_render))  # type: ignore

        if params != repo.params:
            response["HX-Push-Url"] = "?" + repo.params.urlencode()

        return response
