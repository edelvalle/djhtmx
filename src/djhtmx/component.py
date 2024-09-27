from __future__ import annotations

import logging
import re
import typing as t
from collections import defaultdict
from dataclasses import dataclass, field as dataclass_field
from functools import cache, cached_property
from itertools import chain
from os.path import basename

from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser, AnonymousUser
from django.db import models
from django.http import HttpRequest, HttpResponse
from django.shortcuts import resolve_url
from django.template import Context, loader
from django.utils.safestring import SafeString, mark_safe
from pydantic import BaseModel, ConfigDict, Field, validate_call
from pydantic.fields import ModelPrivateAttr
from typing_extensions import deprecated

from . import json
from .introspection import annotate_model, get_event_handler_event_types, get_function_parameters
from .query import Query, QueryPatcher
from .tracing import sentry_span
from .utils import generate_id

__all__ = ("Component", "PydanticComponent", "Query", "ComponentNotFound")


class ComponentNotFound(LookupError):
    pass


@dataclass(slots=True)
class Destroy:
    component_id: str
    command: t.Literal["destroy"] = "destroy"


@dataclass(slots=True)
class Redirect:
    url: str
    command: t.Literal["redirect"] = "redirect"

    @classmethod
    def to(cls, to: t.Callable[[], t.Any] | models.Model | str, *args, **kwargs):
        return cls(resolve_url(to, *args, **kwargs))


@dataclass(slots=True)
class Focus:
    selector: str
    command: t.Literal["focus"] = "focus"


@dataclass(slots=True)
class Execute:
    component_id: str
    event_handler: str
    event_data: dict[str, t.Any]


@dataclass(slots=True)
class DispatchEvent:
    target: str
    event: str
    detail: t.Any
    bubbles: bool = False
    cancelable: bool = False
    composed: bool = False
    command: t.Literal["dispatch_event"] = "dispatch_event"


@dataclass(slots=True)
class SkipRender:
    component: "PydanticComponent"


@dataclass(slots=True)
class Render:
    component: "PydanticComponent" | tuple[type["PydanticComponent"], dict[str, t.Any]]
    template: str | None = None
    oob: str = "true"

    @property
    def component_id(self):
        if isinstance(self.component, tuple):
            _, state = self.component
            return state.get("id")
        else:
            return self.component.id

    @classmethod
    def append(cls, target_: str, component_: type[PydanticComponent], **state):
        return cls(component=(component_, state), template=None, oob=f"beforeend: {target_}")

    @classmethod
    def prepend(cls, target_: str, component_: type[PydanticComponent], **state):
        return cls(component=(component_, state), template=None, oob=f"afterbegin: {target_}")

    @classmethod
    def after(cls, target_: str, component_: type["PydanticComponent"], **state):
        return cls(component=(component_, state), template=None, oob=f"afterend: {target_}")

    @classmethod
    def before(cls, target_: str, component_: type[PydanticComponent], **state):
        return cls(component=(component_, state), template=None, oob=f"beforeend: {target_}")

    @classmethod
    def update(cls, component: type[PydanticComponent], **state):
        return cls(component=(component, state))


@dataclass(slots=True)
class Emit:
    event: t.Any


@dataclass(slots=True)
class Signal:
    name: str


Command = Destroy | Redirect | Focus | DispatchEvent | SkipRender | Render | Emit | Signal | Execute


RenderFunction = t.Callable[[Context | dict[str, t.Any] | None], SafeString]

PYDANTIC_MODEL_METHODS = {
    attr_name for attr_name in dir(BaseModel) if not attr_name.startswith("_")
}

REGISTRY: dict[str, type[PydanticComponent]] = {}
LISTENERS: dict[type, set[str]] = defaultdict(set)
FQN: dict[type[PydanticComponent], str] = {}


@cache
def _get_query_patchers(component_name: str) -> list[QueryPatcher]:
    return list(QueryPatcher.for_component(REGISTRY[component_name]))


@cache
def _get_querystring_subscriptions(component_name: str) -> frozenset[str]:
    return frozenset({
        patcher.signal_name
        for patcher in _get_query_patchers(component_name)
        if patcher.auto_subscribe
    })


A = t.TypeVar("A")
B = t.TypeVar("B")
P = t.ParamSpec("P")


def _compose(f: t.Callable[P, A], g: t.Callable[[A], B]) -> t.Callable[P, B]:
    def result(*args: P.args, **kwargs: P.kwargs):
        return g(f(*args, **kwargs))

    return result


RENDER_FUNC: dict[str, RenderFunction] = {}


def get_template(template: str) -> RenderFunction:
    if settings.DEBUG:
        return _compose(loader.get_template(template).render, mark_safe)
    else:
        if (render := RENDER_FUNC.get(template)) is None:
            render = _compose(loader.get_template(template).render, mark_safe)
            RENDER_FUNC[template] = render
        return render


class PydanticComponent(BaseModel):
    _template_name: str = ...  # type: ignore

    # fields to exclude from component state during serialization
    model_config = ConfigDict(
        arbitrary_types_allowed=True,
    )

    def __init_subclass__(cls, public=None):
        FQN[cls] = f"{cls.__module__}.{cls.__name__}"

        component_name = cls.__name__

        if public is None:
            if _ABSTRACT_BASE_REGEX.match(component_name):
                logger.info(
                    "HTMX Component: <%s> Automatically detected as non public",
                    FQN[cls],
                )
                public = False
            else:
                public = True

        if public:
            REGISTRY[component_name] = cls

            # Warn of components that do not have event handlers and are public
            if (
                not any(cls.__own_event_handlers(get_parent_ones=True))
                and not hasattr(cls, "_handle_event")
                and not hasattr(cls, "subscriptions")
            ):
                logger.warning(
                    "HTMX Component <%s> has no event handlers, probably should not exist and be just a template",
                    FQN[cls],
                )

        assert isinstance(cls._template_name, ModelPrivateAttr)
        if isinstance(cls._template_name.default, str) and (
            basename(cls._template_name.default)
            not in (f"{klass.__name__}.html" for klass in cls.__mro__)
        ):
            logger.warning(
                "HTMX Component <%s> template name does not match the component name",
                FQN[cls],
            )

        # We use 'get_type_hints' to resolve the forward refs if needed, but
        # we only need to rewrite the actual annotations of the current class,
        # that's why we iter over the '__annotations__' names.
        hints = t.get_type_hints(cls, include_extras=True)
        for name in list(cls.__annotations__):
            if not name.startswith("_"):
                annotation = hints[name]
                cls.__annotations__[name] = annotate_model(annotation)

        for name, event_handler in cls.__own_event_handlers():
            setattr(
                cls,
                name,
                validate_call(config={"arbitrary_types_allowed": True})(event_handler),
            )

        if public and (event_handler := getattr(cls, "_handle_event", None)):
            for event_type in get_event_handler_event_types(event_handler):
                LISTENERS[event_type].add(component_name)

        return super().__init_subclass__()

    @classmethod
    def __own_event_handlers(cls, get_parent_ones=False):
        for attr_name in dir(cls) if get_parent_ones else vars(cls):
            if (
                not attr_name.startswith("_")
                and attr_name not in PYDANTIC_MODEL_METHODS
                and attr_name.islower()
                and callable(attr := getattr(cls, attr_name, None))
            ):
                yield attr_name, attr

    # State
    id: t.Annotated[str, Field(default_factory=generate_id)]
    user: t.Annotated[AnonymousUser | AbstractBaseUser, Field(exclude=True)]
    hx_name: str

    @property
    def subscriptions(self) -> set[str]:
        return set()

    def _get_all_subscriptions(self) -> set[str]:
        return self.subscriptions | _get_querystring_subscriptions(self.hx_name)

    def _get_template(self, template: str | None = None) -> t.Callable[..., SafeString]:
        return get_template(template or self._template_name)

    def _get_context(self):
        with sentry_span(f"{FQN[type(self)]}._get_context"):
            return {
                attr: getattr(self, attr)
                for attr in dir(self)
                if not attr.startswith("_") and attr not in PYDANTIC_MODEL_METHODS
            } | {"this": self}


@dataclass(slots=True)
class Triggers:
    """HTMX triggers.

    Allow to trigger events on the client from the server.  See
    https://htmx.org/attributes/hx-trigger/

    """

    _trigger: dict[str, list[t.Any]] = dataclass_field(default_factory=lambda: defaultdict(list))
    _after_swap: dict[str, list[t.Any]] = dataclass_field(default_factory=lambda: defaultdict(list))
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


@deprecated("Use PydanticComponent")
class Component:
    template_name = ""
    template = None
    _all = {}
    _urls = {}
    _name = ...
    _fields: tuple[str, ...]

    _pydantic_config = ConfigDict({
        "arbitrary_types_allowed": True,
    })

    def __init_subclass__(cls, name=None, public=True):
        if public:
            name = name or cls.__name__
            Component._all[name] = cls
            cls._name = name

        cls._fields = get_function_parameters(cls.__init__, exclude={"self"})

        for attr_name in dict(vars(cls)):
            attr = getattr(cls, attr_name)
            if attr_name == "__init__" or (
                not attr_name.startswith("_") and attr_name.islower() and callable(attr)
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
    def user(self) -> AbstractBaseUser | AnonymousUser:
        user = getattr(self.request, "user", None)
        if user is None or not isinstance(user, AbstractBaseUser):
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
                {attr: getattr(self, attr) for attr in dir(self) if not attr.startswith("_")},
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


logger = logging.getLogger(__name__)


_ABSTRACT_BASE_REGEX = re.compile(r"^(_)?(Base|Abstract)[A-Z0-9_]")
