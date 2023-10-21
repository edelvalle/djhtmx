from functools import cached_property
import typing as t
from collections import defaultdict
from urllib.parse import urlparse
from uuid import uuid4

from django.conf import settings
from django.db import models
from django.http import HttpRequest, HttpResponse, QueryDict
from django.shortcuts import resolve_url
from django.template import loader
from django.utils.safestring import SafeString, mark_safe
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    validate_arguments,
)


from .tracing import sentry_span
from . import json


class ComponentNotFound(LookupError):
    pass


RenderFunction = t.Callable[[dict[str, t.Any]], SafeString]


def get_params(request: HttpRequest) -> QueryDict:
    is_htmx_request = json.loads(request.META.get("HTTP_HX_REQUEST", "false"))
    if is_htmx_request:
        return QueryDict(
            urlparse(request.META['HTTP_HX_CURRENT_URL']).query,
            mutable=True,
        )
    else:
        return request.GET.copy()


class Repository:
    def __init__(
        self,
        request: HttpRequest,
        state_by_id: dict[str, dict[str, t.Any]] = None,
    ):
        self.request = request
        self.component_by_id: dict[str, "Component"] = {}
        self.state_by_id = state_by_id or {}
        self.params = get_params(request)

    def build(self, component_name: str, state: dict[str, t.Any]):
        if component_id := state.get("id"):
            if component := self.component_by_id.get(component_id):
                for key, value in state.items():
                    setattr(component, key, value)
                return component
            elif stored_state := self.state_by_id.pop(component_id, None):
                state = stored_state | state

        component = Component._build(
            component_name, self.request, self.params, state
        )
        return self.register_component(component)

    def register_component(self, component: "Component") -> "Component":
        self.component_by_id[component.id] = component
        return component

    def render(self, component: "Component"):
        return component.controller.render(
            component._get_template(),
            component._get_context() | {"htmx_repo": self},
        )

    def render_html(self, component: "Component"):
        return component.controller.render_html(
            component._get_template(),
            component._get_context() | {"htmx_repo": self},
        )


class Controller:
    def __init__(self, request: HttpRequest, params: QueryDict):
        self.request = request
        self.params = params
        self._destroyed: bool = False
        self._headers: dict[str, str] = {}

    def destroy(self):
        self._destroyed = True

    @cached_property
    def triggers(self):
        return Triggers()

    def redirect_to(self, url, **kwargs):
        self._headers["HX-Redirect"] = resolve_url(url, **kwargs)

    def focus(self, selector):
        self.triggers.after_settle('hxFocus', selector)

    def dispatch_event(self, target: str, event: str):
        self.triggers.after_settle(
            'hxDispatchEvent',
            {
                'target': target,
                'event': event,
            },
        )

    def render(self, render: RenderFunction, context: dict[str, t.Any]):
        response = HttpResponse(self.render_html(render, context))
        for key, value in (self._headers | self.triggers.headers).items():
            response[key] = value
        return response

    def render_html(self, render: RenderFunction, context: dict[str, t.Any]):
        if self._destroyed:
            html = ''
        else:
            html = render(context | {"request": self.request}).strip()
        return mark_safe(html)


def Model(model: t.Type[models.Model]):
    return t.Annotated[
        model,
        BeforeValidator(
            lambda v: v
            if isinstance(v, model)
            else model.objects.filter(pk=v).first()
        ),
        PlainSerializer(
            lambda v: v.pk,
            int if (pk := model().pk) is None else type(pk),
        ),
    ]


REGISTRY: dict[str, t.Type["Component"]] = {}
FQN: dict[t.Type["Component"], str] = {}
RENDER_FUNC: dict[str, RenderFunction] = {}


class Component(BaseModel):
    __name__: str
    _template_name: str = ...  # type: ignore

    # fields to exclude from component state during serialization
    _exclude_fields = {"controller"}

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
    )

    def __init_subclass__(cls, public=True):
        FQN[cls] = cls.__name__
        if public:
            REGISTRY[cls.__name__] = cls

        for name, annotation in list(cls.__annotations__.items()):
            if issubclass(annotation, models.Model):
                cls.__annotations__[name] = Model(annotation)

        for attr_name in vars(cls):
            attr = getattr(cls, attr_name)
            if (
                not (
                    attr_name.startswith("_") or attr_name.startswith("model_")
                )
                and attr_name.islower()
                and callable(attr)
            ):
                setattr(
                    cls,
                    attr_name,
                    validate_arguments(
                        config={"arbitrary_types_allowed": True}
                    )(attr),
                )

        return super().__init_subclass__()

    @classmethod
    def _build(
        cls,
        _component_name: str,
        request: HttpRequest,
        params: QueryDict,
        state: dict[str, t.Any],
    ):
        if _component_name not in REGISTRY:
            raise ComponentNotFound(
                f"Could not find requested component '{_component_name}'. "
                "Did you load the component?"
            )

        return REGISTRY[_component_name](
            **dict(state, controller=Controller(request, params))
        )

    def _get_template(self) -> t.Callable[..., SafeString]:
        template = self._template_name
        if settings.DEBUG:
            return loader.get_template(template).render
        else:
            if (render := RENDER_FUNC.get(template)) is None:
                render = loader.get_template(template).render
                RENDER_FUNC[template] = render
            return render

    # State
    id: str = Field(default_factory=lambda: f'hx-{uuid4().hex}')
    controller: Controller

    def _get_context(self):
        with sentry_span(f"{FQN[type(self)]}._get_context"):
            return {
                attr: getattr(self, attr)
                for attr in dir(self)
                if not attr.startswith('_')
            } | {"this": self}


class Triggers:
    def __init__(self):
        self._trigger = defaultdict(list)
        self._after_swap = defaultdict(list)
        self._after_settle = defaultdict(list)

    def add(self, name, what: t.Any):
        self._trigger[name].append(what)

    def after_swap(self, name, what: t.Any):
        self._after_swap[name].append(what)

    def after_settle(self, name, what: t.Any):
        self._after_settle[name].append(what)

    @property
    def headers(self):
        headers = [
            ('HX-Trigger', self._trigger),
            ('HX-Trigger-After-Swap', self._after_swap),
            ('HX-Trigger-After-Settle', self._after_settle),
        ]
        return {header: json.dumps(value) for header, value in headers if value}
