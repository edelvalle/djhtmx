from __future__ import annotations

import inspect
import logging
import re
import time
import typing as t
from collections import defaultdict
from dataclasses import dataclass, field as dataclass_field
from enum import IntEnum
from functools import cache, cached_property, partial
from os.path import basename

from django.contrib.auth.models import AbstractBaseUser
from django.db import models
from django.shortcuts import resolve_url
from django.template import Context, loader
from django.utils.safestring import SafeString, mark_safe
from pydantic import BaseModel, ConfigDict, Field, validate_call
from pydantic.fields import ModelPrivateAttr

from . import json, settings
from .introspection import (
    Unset,
    annotate_model,
    get_event_handler_event_types,
    get_function_parameters,
)
from .query import Query, QueryPatcher
from .tracing import sentry_span
from .utils import generate_id

__all__ = ("HtmxComponent", "Query", "ComponentNotFound")


class ComponentNotFound(LookupError):
    pass


@dataclass(slots=True)
class Destroy:
    "Destroys the given component in the browser and in the caches."

    component_id: str
    command: t.Literal["destroy"] = "destroy"


@dataclass(slots=True)
class Redirect:
    "Executes a browser redirection to the given URL."

    url: str
    command: t.Literal["redirect"] = "redirect"

    @classmethod
    def to(cls, to: t.Callable[[], t.Any] | models.Model | str, *args, **kwargs):
        return cls(resolve_url(to, *args, **kwargs))


@dataclass(slots=True)
class Open:
    "Open a new window with the URL."

    url: str
    name: str = ""
    rel: str = "noopener noreferrer"
    target: str = "_blank"

    command: t.Literal["open-tab"] = "open-tab"

    @classmethod
    def to(cls, to: t.Callable[[], t.Any] | models.Model | str, *args, **kwargs):
        return cls(resolve_url(to, *args, **kwargs))


@dataclass(slots=True)
class Focus:
    "Executes a '.focus()' on the browser element that matches `selector`"

    selector: str
    command: t.Literal["focus"] = "focus"


@dataclass(slots=True)
class Execute:
    component_id: str
    event_handler: str
    event_data: dict[str, t.Any]


@dataclass(slots=True)
class DispatchDOMEvent:
    "Dispatches a DOM CustomEvent in the given target."

    target: str
    event: str
    detail: t.Any
    bubbles: bool = False
    cancelable: bool = False
    composed: bool = False
    command: t.Literal["dispatch_dom_event"] = "dispatch_dom_event"


@dataclass(slots=True)
class SkipRender:
    "Instruct the HTMX engine to avoid the render of the component."

    component: "HtmxComponent"


@dataclass(slots=True)
class BuildAndRender:
    component: type["HtmxComponent"]
    state: dict[str, t.Any]
    oob: str = "true"
    timestamp: int = dataclass_field(default_factory=time.monotonic_ns)

    @classmethod
    def append(cls, target_: str, component_: type[HtmxComponent], **state):
        return cls(component=component_, state=state, oob=f"beforeend: {target_}")

    @classmethod
    def prepend(cls, target_: str, component_: type[HtmxComponent], **state):
        return cls(component=component_, state=state, oob=f"afterbegin: {target_}")

    @classmethod
    def after(cls, target_: str, component_: type[HtmxComponent], **state):
        return cls(component=component_, state=state, oob=f"afterend: {target_}")

    @classmethod
    def before(cls, target_: str, component_: type[HtmxComponent], **state):
        return cls(component=component_, state=state, oob=f"beforebegin: {target_}")

    @classmethod
    def update(cls, component: type[HtmxComponent], **state):
        return cls(component=component, state=state)


@dataclass(slots=True)
class Render:
    component: HtmxComponent
    template: str | None = None
    oob: str = "true"
    lazy: bool | None = None
    timestamp: int = dataclass_field(default_factory=time.monotonic_ns)


@dataclass(slots=True)
class Emit:
    "Emit a backend-only event."

    event: t.Any
    timestamp: int = dataclass_field(default_factory=time.monotonic_ns)


@dataclass(slots=True)
class Signal:
    "Emit a backend-only signal."

    names: set[tuple[str, str]]  # set[tuple[signal name, emitter component id]]
    timestamp: int = dataclass_field(default_factory=time.monotonic_ns)


Command = (
    Destroy
    | Redirect
    | Focus
    | DispatchDOMEvent
    | SkipRender
    | BuildAndRender
    | Render
    | Emit
    | Signal
    | Execute
    | Open
)


RenderFunction = t.Callable[[Context | dict[str, t.Any] | None], SafeString]

PYDANTIC_MODEL_METHODS = {
    attr_name for attr_name in dir(BaseModel) if not attr_name.startswith("_")
}

REGISTRY: dict[str, type[HtmxComponent]] = {}
LISTENERS: dict[type, set[str]] = defaultdict(set)
FQN: dict[type[HtmxComponent], str] = {}


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


class HandlerType(IntEnum):
    SYNC = 0
    GENERATOR = 1
    ASYNC = 2
    ASYNC_GENERATOR = 3

    @classmethod
    def from_function(cls, func: t.Callable) -> HandlerType:
        if inspect.isgeneratorfunction(func):
            return cls.GENERATOR
        elif inspect.iscoroutinefunction(func):
            return cls.ASYNC
        elif inspect.isasyncgenfunction(func):
            return cls.ASYNC_GENERATOR
        else:
            return cls.SYNC


def get_template(template: str) -> RenderFunction:  # pragma: no cover
    if settings.DEBUG:
        return _compose(loader.get_template(template).render, mark_safe)
    else:
        if (render := RENDER_FUNC.get(template)) is None:
            render = _compose(loader.get_template(template).render, mark_safe)
            RENDER_FUNC[template] = render
        return render


class HtmxComponent(BaseModel):
    _template_name: str = ...  # type: ignore
    _template_name_lazy: str = settings.DEFAULT_LAZY_TEMPLATE

    # tracks which attributes are properties, to expose them in a lazy way to the _get_context
    # during rendering
    _properties: set[str] = ...  # type: ignore

    # tracks what are the names of the event handlers of the class
    _event_handler_params: dict[str, frozenset[str]] = ...  # type: ignore

    # fields to exclude from component state during serialization
    model_config = ConfigDict(
        arbitrary_types_allowed=True,
    )

    def __init_subclass__(cls, public=None):
        FQN[cls] = f"{cls.__module__}.{cls.__name__}"

        component_name = cls.__name__

        if public is None:
            # Detect concrete versions of generic classes, they are non public
            if "[" in component_name and "]" in component_name:
                public = False
            elif _ABSTRACT_BASE_REGEX.match(component_name):
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

        cls._event_handler_params = {
            name: get_function_parameters(event_handler)
            for name, event_handler in cls.__own_event_handlers(get_parent_ones=True)
        }

        for name, params in cls._event_handler_params.items():
            attr = getattr(cls, name)
            attr.handler_type = HandlerType.from_function(attr)
            if params and not hasattr(attr, "raw_function"):
                new_attr = validate_call(config={"arbitrary_types_allowed": True})(attr)
                new_attr.handler_type = attr.handler_type
                setattr(
                    cls,
                    name,
                    new_attr,
                )

        cls.__check_consistent_event_handler(strict=settings.STRICT_EVENT_HANDLER_CONSISTENCY_CHECK)
        if public:
            if event_handler := getattr(cls, "_handle_event", None):
                event_handler.handler_type = HandlerType.from_function(event_handler)
                for event_type in get_event_handler_event_types(event_handler):
                    LISTENERS[event_type].add(component_name)

            cls._properties = {
                attr
                for attr in dir(cls)
                if not attr.startswith("_")
                if attr not in PYDANTIC_MODEL_METHODS
                if isinstance(getattr(cls, attr), (property, cached_property))
            }

        return super().__init_subclass__()

    @classmethod
    def __own_event_handlers(cls, get_parent_ones=False):
        attr_names = dir(cls) if get_parent_ones else vars(cls)
        for attr_name in attr_names:
            if (
                not attr_name.startswith("_")
                and attr_name not in PYDANTIC_MODEL_METHODS
                and attr_name.islower()
                and callable(attr := getattr(cls, attr_name))
            ):
                yield attr_name, attr

    @classmethod
    def __check_consistent_event_handler(cls, *, strict: bool = False):
        """Check that '_handle_event' is consistent.

        If the class inherits from one that super-class, and it gets
        `_handle_event` from several of those branches, it must override it to
        resolve the ambiguity.

        Raise an error if there is no self-defined method.

        """
        parents = {
            method
            for base in cls.__bases__
            if (method := getattr(base, "_handle_event", None)) is not None
        }
        if len(parents) > 1:
            resolved = getattr(cls, "_handle_event")
            if resolved in parents:
                bases = ", ".join(
                    base.__name__
                    for base in cls.__bases__
                    if (method := getattr(base, "_handle_event", None)) is not None
                )
                if strict:
                    raise TypeError(
                        f"Component {cls.__name__} doesn't override "
                        f"_handle_event to reconcile the base classes ({bases})."
                    )
                else:
                    logger.error(
                        "Component %s doesn't override _handle_event to reconcile the base classes (%s)",
                        cls.__name__,
                        bases,
                    )

    # State
    id: t.Annotated[str, Field(default_factory=generate_id)]
    user: t.Annotated[AbstractBaseUser | None, Field(exclude=True)]
    hx_name: str
    lazy: bool = False

    def __repr__(self) -> str:
        return f"{self.hx_name}(\n{self.model_dump_json(indent=2, exclude={'hx_name'})})\n"

    @property
    def subscriptions(self) -> set[str]:
        return set()

    def render(self): ...

    def _get_all_subscriptions(self) -> set[str]:
        return self.subscriptions | _get_querystring_subscriptions(self.hx_name)

    def _get_template(self, template: str | None = None) -> t.Callable[..., SafeString]:
        return get_template(template or self._template_name)

    def _get_lazy_context(self):
        return {}

    def _get_context(self):
        # This render-local cache, supports lazy properties but avoids the same property to be
        # computed more than once.  It doesn't survive several renders which is good, because it
        # doesn't require invalidation.
        def get_property(cache, attr):
            result = cache.get(attr, Unset)
            if result is Unset:
                result = getattr(self, attr)
                cache[attr] = result
            return result

        with sentry_span(f"{FQN[type(self)]}._get_context"):
            render_cache = {}
            return {
                attr: (
                    partial(get_property, render_cache, attr)  # do lazy evaluation of properties
                    if attr in self._properties
                    else getattr(self, attr)
                )
                for attr in dir(self)
                if not attr.startswith("_") and attr not in PYDANTIC_MODEL_METHODS
            }


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


logger = logging.getLogger(__name__)


_ABSTRACT_BASE_REGEX = re.compile(r"^(_)?(Base|Abstract)[A-Z0-9_]")
