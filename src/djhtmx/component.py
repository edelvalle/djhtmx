from __future__ import annotations

import typing as t
from collections import defaultdict
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from functools import cached_property
from itertools import chain
from pprint import pprint
from urllib.parse import urlparse
from uuid import uuid4

from django.conf import settings
from django.contrib.auth.models import AbstractUser, AnonymousUser
from django.db import models
from django.db.models.signals import post_save, pre_delete
from django.http import Http404, HttpRequest, HttpResponse, QueryDict
from django.shortcuts import resolve_url
from django.template import Context, loader
from django.utils.html import format_html
from django.utils.safestring import SafeString, mark_safe
from pydantic import BaseModel, ConfigDict, Field, validate_call

from . import json
from .introspection import (
    annotate_model,
    get_event_handler_event_types,
    get_function_parameters,
    get_related_fields,
)
from .query import Query, QueryPatcher
from .tracing import sentry_span

__all__ = ("Component", "PydanticComponent", "Query", "ComponentNotFound")


class ComponentNotFound(LookupError):
    pass


TUser = t.TypeVar("TUser", bound=AbstractUser)
RenderFunction = t.Callable[
    [Context | dict[str, t.Any] | None, HttpRequest | None], SafeString
]


def get_params(request: HttpRequest) -> QueryDict:
    is_htmx_request = json.loads(request.META.get("HTTP_HX_REQUEST", "false"))
    if is_htmx_request:
        return QueryDict(  # type: ignore
            urlparse(request.META["HTTP_HX_CURRENT_URL"]).query,
            mutable=True,
        )
    else:
        return request.GET.copy()


class RequestWithRepo(HttpRequest):
    djhtmx: Repository


PyComp = t.TypeVar("PyComp", bound="PydanticComponent")


class Repository:
    """An in-memory (cheap) mapping of component IDs to its states.

    When an HTMX request comes, all the state from all the components are
    placed in a registry.  This way we can instantiate components if/when
    needed.

    For instance, if a component is subscribed to an event and the event fires
    during the request, that component is rendered.

    """

    @classmethod
    def from_request(
        cls,
        request: RequestWithRepo,
        state_by_id: dict[str, dict[str, t.Any]] = None,
        subscriptions_by_id: dict[str, list[str]] = None,
    ) -> Repository:
        if not hasattr(request, "djhtmx"):
            request.djhtmx = cls(
                request,
                state_by_id=state_by_id,
                subscriptions_by_id=subscriptions_by_id,
            )
        return request.djhtmx

    def __init__(
        self,
        request: RequestWithRepo,
        state_by_id: dict[str, dict[str, t.Any]] = None,
        subscriptions_by_id: dict[str, list[str]] = None,
    ):
        self.request = request
        self.component_by_id: dict[str, PydanticComponent] = {}
        self.component_by_name: dict[str, list[PydanticComponent]] = (
            defaultdict(list)
        )
        self.state_by_id = state_by_id or {}
        self.subscriptions_by_id = subscriptions_by_id or {}

        self.params = get_params(request)
        self.signals = set()

        if self.subscriptions_by_id:
            post_save.connect(
                receiver=self._listen_to_post_save,
            )
            pre_delete.connect(
                receiver=self._listen_to_pre_delete,
            )

    def unlink(self):
        """Remove circular references to ensure GC deallocates me"""
        for component in self.component_by_id.values():
            delattr(component.controller, "request")
            delattr(component.controller, "params")
            delattr(component, "controller")
        delattr(self, "request")
        delattr(self, "params")
        delattr(self, "component_by_id")
        delattr(self, "component_by_name")

    def _listen_to_post_save(
        self,
        sender: type[models.Model],
        instance: models.Model,
        created: bool,
        **kwargs,
    ):
        app = sender._meta.app_label
        name = sender._meta.model_name
        self.signals.update([f"{app}.{name}", f"{app}.{name}.{instance.pk}"])
        action = "created" if created else "updated"
        self.signals.add(f"{app}.{name}.{instance.pk}.{action}")
        self._listen_to_related(sender, instance, action=action)

    def _listen_to_pre_delete(
        self,
        sender: type[models.Model],
        instance: models.Model,
        **kwargs,
    ):
        app = sender._meta.app_label
        name = sender._meta.model_name
        self.signals.update(
            [
                f"{app}.{name}",
                f"{app}.{name}.{instance.pk}",
                f"{app}.{name}.{instance.pk}.deleted",
            ]
        )
        self._listen_to_related(sender, instance, action="deleted")

    def _listen_to_related(
        self,
        sender: type[models.Model],
        instance: models.Model,
        action: str,
    ):
        for field in get_related_fields(sender):
            fk_id = getattr(instance, field.name)
            signal = f"{field.related_model_name}.{fk_id}.{field.relation_name}"
            self.signals.update((signal, f"{signal}.{action}"))

    def dispatch_signals(self, ignore_components: set[str] | None = None):
        if not ignore_components:
            ignore_components = set()

        if settings.DEBUG and self.signals:
            print("LAUNCHED SIGNALS:")
            pprint(self.signals)

        components_to_update = set()

        # Model mutation signals
        for component_id, subscriptions in self.subscriptions_by_id.items():
            if component_id in ignore_components:
                continue  # HAHAHA!!! 🤯

            if (
                self.signals.intersection(subscriptions)
                and (component := self.get_component_by_id(component_id))
                is not None
            ):
                if settings.DEBUG:
                    print("> MATCHED: ", component.hx_name, subscriptions)
                components_to_update.add(component.id)

        # Events emitted
        more_to_come = True
        while more_to_come:
            more_to_come = False
            for component_id, component in list(self.component_by_id.items()):
                if component_id in ignore_components:
                    continue  # HAHAHA!!! 🤯

                for event in component.controller.consume_events():
                    more_to_come = True
                    for component_name in LISTENERS[type(event)]:
                        for component in self.get_components_by_name(
                            component_name
                        ):
                            component._handle_event(event)  # type: ignore
                            components_to_update.add(component.id)

        # Rendering
        for component_id in components_to_update:
            component = self.get_component_by_id(component_id)
            assert component
            yield self.render_html(component, oob="true")

    def render_oob(self):
        # component_by_id can change size during iteration
        for component in list(self.component_by_id.values()):
            for oob, component in component.controller._oob:
                yield self.render_html(component, oob=oob)

    def build(self, component_name: str, state: dict[str, t.Any]):
        if component_id := state.get("id"):
            if component := self.component_by_id.get(component_id):
                for key, value in state.items():
                    setattr(component, key, value)
                return component
            elif stored_state := self.state_by_id.pop(component_id, None):
                state = stored_state | state

        state = self._patch_state_with_query_string(component_name, state)
        component = build(component_name, self.request, self.params, state)
        return self.register_component(component)

    def get_component_by_id(self, component_id: str):
        if component_id in self.state_by_id:
            state = self.state_by_id.pop(component_id)
            return self.build(state["hx_name"], state)
        else:
            return self.component_by_id.get(component_id)

    def get_components_by_name(
        self, name: str
    ) -> t.Iterator["PydanticComponent"]:
        yield from self.component_by_name[name]
        for state in list(self.state_by_id.values()):
            if state["hx_name"] == name:
                yield self.build(name, state)

    def register_component(self, component: PyComp) -> PyComp:
        self.component_by_id[component.id] = component
        self.component_by_name[type(component).__name__].append(component)
        return component

    def render(
        self, component: PydanticComponent, template: str | None = None
    ) -> HttpResponse:
        return component.controller.render(
            component._get_template(template),
            component._get_context() | {"htmx_repo": self},
        )

    def render_html(
        self,
        component: PydanticComponent,
        oob: str = None,
    ) -> SafeString:
        is_oob = oob not in ("true", None)
        html = [
            format_html('<div hx-swap-oob="{oob}">', oob=oob)
            if is_oob
            else None,
            component.controller.render_html(
                component._get_template(),
                component._get_context()
                | {"htmx_repo": self, "hx_oob": None if is_oob else oob},
            ),
            "</div>" if is_oob else None,
        ]
        return mark_safe("".join(filter(None, html)))

    def _patch_state_with_query_string(self, component_name, state):
        """Patches the state with the component's query annotated fields"""
        if patchers := QS_MAP.get(component_name):
            for patcher in patchers:
                state = state | patcher.get_state_updates(self.params)
        return state


class Controller:
    def __init__(self, request: RequestWithRepo, params: QueryDict):
        self.request = request
        self.params = params
        self._destroyed: bool = False
        self._headers: dict[str, str] = {}
        self._oob: list[tuple[str, PydanticComponent]] = []
        self._events: list[t.Any] = []

    def emit(self, event: t.Any):
        self._events.append(event)

    def consume_events(self):
        event = self._events
        self._events = []
        return event

    def build(self, component: type[PydanticComponent] | str, **state):
        if isinstance(component, type):
            component = component.__name__
        return self.request.djhtmx.build(component, state)

    def destroy(self):
        self._destroyed = True

    def append(self, target: str, component: type[PydanticComponent], **state):
        self._oob.append(
            (f"beforeend:{target}", self.build(component, **state))
        )

    def prepend(self, target: str, component: type[PydanticComponent], **state):
        self._oob.append(
            (f"afterbegin:{target}", self.build(component, **state))
        )

    def after(self, target: str, component: type["PydanticComponent"], **state):
        self._oob.append((f"afterend:{target}", self.build(component, **state)))

    def before(self, target: str, component: type[PydanticComponent], **state):
        self._oob.append(
            (f"beforebegin:{target}", self.build(component, **state))
        )

    def update(self, component: type[PydanticComponent], **state):
        self._oob.append(("true", self.build(component, **state)))

    @cached_property
    def triggers(self):
        return Triggers()

    def redirect_to(
        self,
        url: t.Callable[..., t.Any] | models.Model | str,
        **kwargs,
    ):
        self._headers["HX-Redirect"] = resolve_url(url, **kwargs)

    def focus(self, selector):
        self.triggers.after_settle("hxFocus", selector)

    def dispatch_event(self, target: str, event: str):
        self.triggers.after_settle(
            "hxDispatchEvent",
            {
                "target": target,
                "event": event,
            },
        )

    def render(self, render: RenderFunction, context: dict[str, t.Any]):
        response = HttpResponse(self.render_html(render, context))
        for key, value in (self._headers | self.triggers.headers).items():
            response[key] = value
        return response

    def render_html(self, render: RenderFunction, context: dict[str, t.Any]):
        if self._destroyed:
            html = ""
        else:
            html = render(context, self.request).strip()
        return mark_safe(html)


REGISTRY: dict[str, type[PydanticComponent]] = {}
LISTENERS: dict[type, set[str]] = defaultdict(set)
FQN: dict[type[PydanticComponent], str] = {}
RENDER_FUNC: dict[str, RenderFunction] = {}

# Mapping from component name to the list of the patcher of the internal
# state from query string.
QS_MAP: dict[str, list[QueryPatcher]] = defaultdict(list)


A = t.TypeVar("A")
B = t.TypeVar("B")
P = t.ParamSpec("P")


def _compose(f: t.Callable[P, A], g: t.Callable[[A], B]) -> t.Callable[P, B]:
    def result(*args: P.args, **kwargs: P.kwargs):
        return g(f(*args, **kwargs))

    return result


def get_template(template: str) -> RenderFunction:
    if settings.DEBUG:
        return _compose(loader.get_template(template).render, mark_safe)
    else:
        if (render := RENDER_FUNC.get(template)) is None:
            render = _compose(loader.get_template(template).render, mark_safe)
            RENDER_FUNC[template] = render
        return render


def build(
    component_name: str,
    request: RequestWithRepo,
    params: QueryDict,
    state: dict[str, t.Any],
):
    if component_name not in REGISTRY:
        raise ComponentNotFound(
            f"Could not find requested component '{component_name}'. "
            "Did you load the component?"
        )

    return REGISTRY[component_name](
        **dict(  # type: ignore
            state,
            hx_name=component_name,
            controller=Controller(request, params),
        )
    )


def _generate_uuid():
    return f"hx-{uuid4().hex}"


class PydanticComponent(BaseModel, t.Generic[TUser]):
    _template_name: str = ...  # type: ignore

    # fields to exclude from component state during serialization
    model_config = ConfigDict(
        arbitrary_types_allowed=True,
    )

    def __init_subclass__(cls, public=True):
        FQN[cls] = f"{cls.__module__}.{cls.__name__}"

        component_name = cls.__name__
        if public:
            REGISTRY[component_name] = cls

        if public:
            # We settle the query string patchers before any other processing,
            # because we need the simplest types of the fields.
            cls._settle_querystring_patchers(component_name)

        # We use 'get_type_hints' to resolve the forward refs if needed, but
        # we only need to rewrite the actual annotations of the current class,
        # that's why we iter over the '__annotations__' names.
        hints = t.get_type_hints(cls, include_extras=True)
        for name in list(cls.__annotations__):
            if not name.startswith("_"):
                annotation = hints[name]
                cls.__annotations__[name] = annotate_model(annotation)

        for attr_name in vars(cls):
            attr = getattr(cls, attr_name)
            if (
                not attr_name.startswith("_")
                and attr_name not in PYDANTIC_MODEL_METHODS
                and attr_name.islower()
                and callable(attr)
            ):
                setattr(
                    cls,
                    attr_name,
                    validate_call(config={"arbitrary_types_allowed": True})(
                        attr
                    ),
                )

        if public and (event_handler := getattr(cls, "_handle_event", None)):
            for event_type in get_event_handler_event_types(event_handler):
                LISTENERS[event_type].add(component_name)

        return super().__init_subclass__()

    @classmethod
    def _settle_querystring_patchers(cls, component_name):
        """Updates the mapping to track query strings."""
        QS_MAP[component_name] = QueryPatcher.for_component(cls)

    # State
    id: t.Annotated[str, Field(default_factory=_generate_uuid)]
    controller: t.Annotated[Controller, Field(exclude=True)]

    hx_name: str

    @classmethod
    def _build(cls, controller: Controller, **state):
        return cls(controller=controller, hx_name=cls.__name__, **state)

    @property
    def subscriptions(self) -> set[str]:
        return set()

    def get_all_subscriptions(self) -> set[str]:
        result = self.subscriptions
        query_patchers = QS_MAP.get(self.hx_name, [])
        query_subscriptions = {
            f"querystring.{p.qs_arg}" for p in query_patchers
        }
        return result | query_subscriptions

    @cached_property
    def user(self) -> TUser:
        if isinstance(self.any_user, AnonymousUser):
            raise Http404()
        else:
            return self.any_user

    @cached_property
    def any_user(self) -> TUser | AnonymousUser:
        user = getattr(self.controller.request, "user", None)
        if user is None:
            return AnonymousUser()
        else:
            return user

    def _get_template(
        self, template: str | None = None
    ) -> t.Callable[..., SafeString]:
        return get_template(template or self._template_name)

    def _get_context(self):
        attrs_to_exclude = {"user", "any_user"}
        with sentry_span(f"{FQN[type(self)]}._get_context"):
            return {
                attr: getattr(self, attr)
                for attr in dir(self)
                if not attr.startswith("_")
                and attr not in PYDANTIC_MODEL_METHODS
                and attr not in attrs_to_exclude
            } | {"this": self}


@dataclass(slots=True)
class Triggers:
    """HTMX triggers.

    Allow to trigger events on the client from the server.  See
    https://htmx.org/attributes/hx-trigger/

    """

    _trigger: dict[str, list[t.Any]] = dataclass_field(
        default_factory=lambda: defaultdict(list)
    )
    _after_swap: dict[str, list[t.Any]] = dataclass_field(
        default_factory=lambda: defaultdict(list)
    )
    _after_settle: dict[str, list[t.Any]] = dataclass_field(
        default_factory=lambda: defaultdict(list)
    )

    def add(self, name, what: t.Any):
        self._trigger[name].append(what)

    def after_swap(self, name, what: t.Any):
        self._after_swap[name].append(what)

    def after_settle(self, name, what: t.Any):
        self._after_settle[name].append(what)

    @property
    def headers(self):
        headers = [
            ("HX-Trigger", self._trigger),
            ("HX-Trigger-After-Swap", self._after_swap),
            ("HX-Trigger-After-Settle", self._after_settle),
        ]
        return {header: json.dumps(value) for header, value in headers if value}


# Legacy Components


class Component:
    template_name = ""
    template = None
    _all = {}
    _urls = {}
    _name = ...
    _fields: tuple[str, ...]

    _pydantic_config = ConfigDict(
        {
            "arbitrary_types_allowed": True,
        }
    )

    def __init_subclass__(cls, name=None, public=True):
        if public:
            name = name or cls.__name__
            cls._all[name] = cls
            cls._name = name

        cls._fields = get_function_parameters(cls.__init__, exclude={"self"})

        for attr_name in dict(vars(cls)):
            attr = getattr(cls, attr_name)
            if attr_name == "__init__" or (
                not attr_name.startswith("_")
                and attr_name.islower()
                and callable(attr)
            ):
                setattr(
                    cls,
                    attr_name,
                    validate_call(config=cls._pydantic_config)(attr),  # type: ignore
                )

        return super().__init_subclass__()

    @classmethod
    def _build(cls, _component_name, request, id, state):
        if _component_name not in cls._all:
            raise ComponentNotFound(
                f"Could not find requested component '{_component_name}'. Did you load the component?"
            )
        return cls._all[_component_name](**dict(state, id=id, request=request))

    def __init__(self, request: HttpRequest, id: str | None = None):
        self.request = request
        self.id = id
        self._destroyed = False
        self._headers = {}
        self._triggers = Triggers()
        self._oob = []

    @cached_property
    def user(self) -> AbstractUser | AnonymousUser:
        user = getattr(self.request, "user", None)
        if user is None or not isinstance(user, AbstractUser):
            return AnonymousUser()
        return user

    @property
    def _state_json(self) -> str:
        return json.dumps({name: getattr(self, name) for name in self._fields})

    def destroy(self):
        self._destroyed = True

    def redirect(self, url, **kwargs):
        self.redirect_raw_url(resolve_url(url, **kwargs))

    def redirect_raw_url(self, url):
        self._headers["HX-Redirect"] = url

    def push_url(self, url, **kwargs):
        self.push_raw_url(resolve_url(url, **kwargs))

    def push_raw_url(self, url):
        self._headers["HX-Push"] = url

    def _send_event(self, target, event):
        self._triggers.after_swap(
            "hxSendEvent",
            {
                "target": target,
                "event": event,
            },
        )

    def _focus(self, selector):
        self._triggers.after_settle("hxFocus", selector)

    def render(self, template: str | None = None):
        response = HttpResponse(self._render(template=template))
        for key, value in (self._headers | self._triggers.headers).items():
            response[key] = value
        return response

    def before_render(self) -> None:
        """Hook called before rendering the template.

        This allows to leave the `__init__` mostly empty, and push some
        computations after initialization just before rendering.  Which plays
        nicer with caching.

        This is your last chance to destroy the component if needed.

        """
        pass

    def _render(self, hx_swap_oob=False, template: str | None = None):
        with sentry_span(f"{self._fqn}._render"):
            with sentry_span(f"{self._fqn}.before_render"):
                self.before_render()
            if self._destroyed:
                html = ""
            else:
                html = mark_safe(
                    self._get_template(template)
                    .render(
                        self._get_context(hx_swap_oob),
                        request=self.request,
                    )
                    .strip()
                )
            if self._oob:
                html = mark_safe(
                    "\n".join(
                        chain(
                            [html],
                            [c._render(hx_swap_oob=True) for c in self._oob],
                        )
                    )
                )
            return html

    def _also_render(self, component, **kwargs):
        self._oob.append(component(request=self.request, **kwargs))

    def _get_template(self, template: str | None = None):
        if template:
            return loader.get_template(template)
        elif not self.template:
            self.template = loader.get_template(self.template_name)
        return self.template

    def _get_context(self, hx_swap_oob):
        with sentry_span(f"{self._fqn}._get_context"):
            return dict(
                {
                    attr: getattr(self, attr)
                    for attr in dir(self)
                    if not attr.startswith("_")
                },
                this=self,
                hx_swap_oob=hx_swap_oob,
            )

    @property
    def _fqn(self) -> str:
        "Fully Qualified Name"
        cls = type(self)
        try:
            mod = cls.__module__
        except AttributeError:
            mod = ""
        name = cls.__name__
        return f"{mod}.{name}" if mod else name


PYDANTIC_MODEL_METHODS = {
    attr
    for attr, value in vars(BaseModel).items()
    if not attr.startswith("_") and callable(value)
}
