from __future__ import annotations

import inspect
import itertools
import logging
import re
import sys
import threading
import types
from collections import defaultdict
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from functools import cache, cached_property, partial, wraps
from inspect import getattr_static, isasyncgenfunction, iscoroutinefunction, isgeneratorfunction
from os.path import basename
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    ClassVar,
    Literal,
    Union,
    cast,
    get_args,
    get_origin,
    get_type_hints,
)

from django.core.exceptions import ImproperlyConfigured
from django.db.models import QuerySet
from django.template import Context, loader
from django.utils.safestring import SafeString, mark_safe
from pydantic import BaseModel, ConfigDict, Field, model_validator, validate_call
from pydantic.fields import ModelPrivateAttr

from . import json, settings
from .exceptions import ComponentNotFound, LoginRequired
from .introspection import (
    ModelConfig,
    Unset,
    annotate_model,
    get_event_handler_event_types,
    get_function_parameters,
)
from .query import Query, QueryPatcher, get_querystring
from .tracing import tracing_span
from .utils import generate_id, get_fqn

try:
    frozendict({})  # type: ignore
except NameError:
    from immutables import Map as frozendict

if TYPE_CHECKING:
    from .sse import SSESubscription

__all__ = (
    "ComponentNotFound",
    "HtmxComponent",
    "LoginRequired",
    "ModelConfig",
    "Query",
    "get_template",
    "is_usable_user",
    "requires_logged_user",
)


RenderFunction = Callable[[Context | dict[str, Any] | None], SafeString]

PYDANTIC_MODEL_METHODS = {
    attr_name for attr_name in dir(BaseModel) if not attr_name.startswith("_")
}

type HandlerKind = Literal["function", "generator"]
"""The shapes an event handler can take, and how each one is consumed.

Each name is the `inspect`:mod: vocabulary for the predicate that recognises the shape, and equally
for what calling such a handler hands back:

`function`
   A plain ``def`` with no ``yield``.  Returns its commands directly, or ``None`` to mean "no
   explicit render", which the pipeline turns into the component's default render.

`generator`
   A ``def`` containing ``yield``.  Calling it returns a generator, which the dispatcher drains to
   exhaustion before acting on any of the commands.

An ``async def`` handler -- a coroutine function or an async generator function -- has no kind of
its own: the dispatcher calls handlers synchronously, so registration refuses one outright instead
of admitting a shape nothing can run.  Teaching dispatch to run them is what would earn them a kind
here.

"""


@dataclass(slots=True, frozen=True)
class _RegisteredComponent:
    """A public component together with what is known about it at import time.

    `handler_kind_mapping` holds the shape of every event handler of the component, including
    `_handle_event` and `_handle_sse_events`, keyed by handler name.

    """

    htmx_component_class: type[HtmxComponent]
    handler_kind_mapping: Mapping[str, HandlerKind]


REGISTRY: dict[str, _RegisteredComponent] = {}
LISTENERS: dict[type, set[str]] = defaultdict(set)
FQN: dict[type[HtmxComponent], str] = {}


@cache
def _get_query_patchers(component_name: str) -> list[QueryPatcher]:
    return list(QueryPatcher.for_component(REGISTRY[component_name].htmx_component_class))


@cache
def _get_querystring_subscriptions(component_name: str) -> frozenset[str]:
    return frozenset({
        patcher.signal_name
        for patcher in _get_query_patchers(component_name)
        if patcher.auto_subscribe
    })


def get_template(template: str) -> RenderFunction:  # pragma: no cover
    if settings.DEBUG:
        return cast(RenderFunction, _compose(loader.get_template(template).render, mark_safe))
    else:
        return _get_template(template)


@cache
def _get_template(template: str) -> RenderFunction:
    return cast(RenderFunction, _compose(loader.get_template(template).render, mark_safe))


class _PerClass[T]:
    """A class attribute whose value `compute` builds on first read, once for each class.

    Each subclass gets its own value: `compute` receives the class through which the attribute is
    read.  Two threads that read it at the same time can both call `compute`; `compute` must then
    have no side effects, so that both results are equal.

    """

    __slots__ = ("compute", "values")

    def __init__(self, compute: Callable[[type], T]):
        self.compute = compute
        self.values: dict[type, T] = {}

    def __get__(self, instance: object, owner: type) -> T:
        try:
            return self.values[owner]
        except KeyError:
            value = self.values[owner] = self.compute(owner)
            return value


class HtmxComponent(BaseModel):
    _template_name: str = ...  # type: ignore
    _template_name_lazy: str = settings.DEFAULT_LAZY_TEMPLATE

    # The names of the attributes that are properties, to expose them in a lazy way to the
    # `_get_context` during rendering.  It is computed on first use, once for each class.
    _properties: ClassVar[frozenset[str]] = _PerClass(lambda cls: _find_properties(cls))  # type: ignore

    # The parameters of each event handler of the class, keyed by handler name.  It is computed on
    # first use, once for each class.
    _event_handler_params: ClassVar[dict[str, frozenset[str]]] = _PerClass(  # type: ignore
        lambda cls: {
            name: get_function_parameters(event_handler)
            for name, event_handler in cls.__own_event_handlers(get_parent_ones=True)
        }
    )

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        # Pydantic builds the validator and serializer of each component on first use, not at
        # import.  An application with hundreds of components starts much faster.
        defer_build=True,
    )

    if TYPE_CHECKING:

        def __init__(self, /, **data: Any) -> None: ...

    def __init_subclass__(cls, public=None):
        """Register the component, and prepare what must exist when the class exists.

        Only the cheap work happens here: the registration of public components, the checks that
        raise for a bad declaration, and the rewrite of the field annotations (pydantic reads them
        right after this method).  The expensive work runs on first use, once for each class: the
        pydantic validator of the component, the validator of each event handler, the parameters of
        the event handlers, and the list of properties.

        """
        FQN[cls] = f"{cls.__module__}.{cls.__name__}"

        component_name = cls.__name__

        if public is None:
            # Detect concrete versions of generic classes, they are non public
            if "[" in component_name and "]" in component_name:
                public = False
            elif _ABSTRACT_BASE_REGEX.match(component_name):
                if settings.STRICT_PUBLIC_BASE:
                    raise TypeError(
                        f"HTMX Component: {FQN[cls]} Automatically detected as non public",
                    )
                logger.info(
                    "HTMX Component: <%s> Automatically detected as non public",
                    FQN[cls],
                )
                public = False
            else:
                public = True

        if public and (existing_component := REGISTRY.get(component_name)):
            raise TypeError(
                f"Component {get_fqn(cls)} would shadow existing "
                f"{get_fqn(existing_component.htmx_component_class)}"
            )

        # One pass over the attributes of the class finds all the event handlers, inherited ones
        # included.  The handlers keep the functions as written: the wrapping comes after.
        event_handlers = dict(cls.__own_event_handlers(get_parent_ones=True))
        cls.__validate_event_handler_calls(event_handlers)

        if public:
            # `__own_event_handlers` skips names starting with `_`, yet `_handle_event` and
            # `_handle_sse_events` reach the dispatcher by the same path as any other handler, so
            # their shape is worth just as much.
            handlers = event_handlers | {
                name: handler
                for name in ("_handle_event", "_handle_sse_events")
                if (handler := getattr(cls, name, None)) is not None
            }

            if async_handlers := sorted(
                name for name, handler in handlers.items() if is_async_handler(handler)
            ):
                raise TypeError(
                    f"Component {get_fqn(cls)} declares async event handlers: "
                    f"{', '.join(async_handlers)}.  djhtmx calls event handlers synchronously, so "
                    "an 'async def' handler never runs: calling it only hands back a coroutine "
                    "(or an async generator) that nothing consumes.  Write it as a plain 'def', "
                    "or as a 'def' that yields its commands."
                )

            REGISTRY[component_name] = _RegisteredComponent(
                htmx_component_class=cls,
                handler_kind_mapping=frozendict({
                    name: get_handler_kind(handler) for name, handler in handlers.items()
                }),
            )

            # Warn of components that do not have event handlers and are public
            if (
                not event_handlers
                and not hasattr(cls, "_handle_event")
                and not hasattr(cls, "subscriptions")
                and not hasattr(cls, "_handle_sse_events")
            ):
                logger.warning(
                    "HTMX Component <%s> has no event handlers, probably should not exist and be just a template",
                    FQN[cls],
                )

        # pydantic >= 2.14 converts the class' own unannotated private attributes only after
        # `__init_subclass__`, so they show up here as plain values.
        match cls._template_name:
            case str(template_name) | ModelPrivateAttr(default=str(template_name)):
                if basename(template_name) not in (
                    f"{klass.__name__}.html" for klass in cls.__mro__
                ):
                    raise ImproperlyConfigured(
                        f"HTMX Component <{FQN[cls]}> template name does not match the component name"
                    )
            case ModelPrivateAttr():
                pass
            case unexpected:
                raise TypeError(f"Unexpected _template_name in {FQN[cls]}: {unexpected!r}")

        for name, annotation in _get_own_field_hints(cls).items():
            cls.__annotations__[name] = annotate_model(annotation)

        cls.__check_consistent_event_handler(strict=settings.STRICT_EVENT_HANDLER_CONSISTENCY_CHECK)
        if public:
            if handle_event := getattr(cls, "_handle_event", None):
                for event_type in get_event_handler_event_types(handle_event, owner=cls):
                    LISTENERS[event_type].add(component_name)

            from .sse import register_sse_listener

            register_sse_listener(cls)

        return super().__init_subclass__()

    @classmethod
    def __own_event_handlers(cls, get_parent_ones=False):
        attr_names = dir(cls) if get_parent_ones else vars(cls)
        for attr_name in attr_names:
            if (
                not attr_name.startswith("_")
                and attr_name not in PYDANTIC_MODEL_METHODS
                and attr_name.islower()
                # A request can only call the methods of a component instance, not of its class.
                and not isinstance(getattr_static(cls, attr_name), classmethod | staticmethod)
                and callable(attr := getattr(cls, attr_name))
            ):
                yield attr_name, attr

    @classmethod
    def __validate_event_handler_calls(cls, event_handlers: Mapping[str, Callable]):
        """Make each event handler with parameters validate its arguments with pydantic.

        A handler that a base class already wrapped keeps its wrapper.  The validator of a plain
        function is built on its first call, not here: see `_validate_call_lazily`.

        """
        for name, handler in event_handlers.items():
            if hasattr(handler, "raw_function"):
                pass
            elif isinstance(handler, types.FunctionType):
                if _declares_parameters(handler):
                    setattr(cls, name, _validate_call_lazily(handler))
            elif get_function_parameters(handler):
                setattr(cls, name, validate_call(config=_VALIDATE_CALL_CONFIG)(handler))

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
            resolved = cls._handle_event  # type: ignore
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
    id: Annotated[str, Field(default_factory=generate_id)]

    user: Annotated[Any | None, Field(exclude=True)]  # type: ignore
    session_id: Annotated[str | None, Field(default=None, exclude=True)] = None
    if TYPE_CHECKING:
        from django.contrib.auth.models import AbstractBaseUser

        user: Annotated[AbstractBaseUser | None, Field(exclude=True)]  # type: ignore

    hx_name: str
    lazy: bool = False

    @model_validator(mode="after")
    def _apply_user_protocol(self):
        """Apply the `user` protocol: a user who cannot act is no user at all.

        A component whose `user` cannot be `None` refuses to exist -- see `requires_logged_user`.
        One that admits `None` gets `None`, so it renders for a visitor with no usable session
        instead of holding a user it must not act as.

        """
        if is_usable_user(self.user):
            return self
        elif requires_logged_user(type(self)):
            raise LoginRequired(get_fqn(type(self)))
        else:
            self.user = None
            return self

    def __repr__(self) -> str:
        return f"{self.hx_name}(\n{self.model_dump_json(indent=2, exclude={'hx_name'})})\n"

    @classmethod
    def get_querystring(cls, **values: Any) -> str:
        """Return the query string that opens a page with this component in the given state.

        See `djhtmx.query.get_querystring`.

        """
        return get_querystring(cls, **values)

    @property
    def subscriptions(self) -> set[str]:
        return set()

    @property
    def sse_subscriptions(self) -> set[SSESubscription]:
        """Return the SSE subscriptions of the component; none by default.

        Subscriptions take effect only when the component defines `_handle_sse_events`, and only
        those whose event type its annotation accepts.

        """
        return set()

    def render(self):
        return

    def _get_all_subscriptions(self) -> set[str]:
        return self.subscriptions | _get_querystring_subscriptions(self.hx_name)

    def _get_template(self, template: str | None = None) -> Callable[..., SafeString]:
        return get_template(template or self._template_name)

    def _get_lazy_context(self):
        return {}

    def _get_context(self):
        # This render-local cache, supports lazy properties but avoids the same property to be
        # computed more than once.  It doesn't survive several renders which is good, because it
        # doesn't require invalidation.
        def materialize(value):
            # Don't put a generator/iterator in the cache, consume it and put the result.
            match value:
                case QuerySet() as query:
                    # queryset has its own cache, we exposed it because the template might use
                    # methods like `exists`, `first`, etc.
                    return query
                case itertools.count() as infinite:
                    # We avoid a hung render for this case only; but we cannot really protect from
                    # infinite iterators in general.  In any case, our cache already affected any
                    # kind of iterator in a property since the templates gets a single one and not
                    # different iterators every time.
                    return infinite
                case tuple() as named if hasattr(named, "_make") and hasattr(named, "_fields"):
                    # DO NOT go defensive.  Imagine someone that subclasses `tuple` and adds a
                    # `_make` that is not what we expect; it will likely fail and it's on them.
                    return named._make(materialize(v) for v in named)
                case tuple() as items:
                    # This branch covers the special case of tuples because of `itertools.groupby`;
                    # it returns an Iterator (caught below) that yields tuples of `(key, iterator)`;
                    # the tuple itself is not an Iterator.  This branch ensures we materialize the
                    # inner iterator.
                    return tuple(materialize(v) for v in items)
                case Iterator() as iterator:
                    return [materialize(v) for v in iterator]
                case other:
                    return other

        def get_property(cache, attr):
            result = cache.get(attr, Unset)
            if result is Unset:
                result = cache[attr] = materialize(getattr(self, attr))
            return result

        with tracing_span(f"{FQN[type(self)]}._get_context"):
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

    _trigger: dict[str, list[Any]] = dataclass_field(default_factory=lambda: defaultdict(list))
    _after_swap: dict[str, list[Any]] = dataclass_field(default_factory=lambda: defaultdict(list))
    _after_settle: dict[str, list[Any]] = dataclass_field(default_factory=lambda: defaultdict(list))

    def add(self, name, what: Any):
        self._trigger[name].append(what)

    def after_swap(self, name, what: Any):
        self._after_swap[name].append(what)

    def after_settle(self, name, what: Any):
        self._after_settle[name].append(what)

    @property
    def headers(self):
        headers = [
            ("HX-Trigger", self._trigger),
            ("HX-Trigger-After-Swap", self._after_swap),
            ("HX-Trigger-After-Settle", self._after_settle),
        ]
        return {header: json.dumps(value) for header, value in headers if value}


def annotated_handler[F](**annotations) -> Callable[[F], F]:
    """Annotate the HTMX handler with customized values.

    Some of these annotations are HtmxUnhandledError use the annotations so that the application can
    have more detailed error recovery handlers.

    """

    def decorator(fn):
        if not hasattr(fn, "_htmx_annotations_"):
            fn._htmx_annotations_ = htmx_annotations = {}
        else:
            htmx_annotations = fn._htmx_annotations_
        htmx_annotations.update(annotations)
        return fn

    return decorator


def is_usable_user(user: Any) -> bool:
    """Whether `user` is someone the request can act as.

    A user must be saved (they own rows), not anonymous, and active.

    """
    if user is None:
        return False
    else:
        try:
            return (
                # `pk` is read first because a `ModelConfig(lazy=True)` user answers it without
                # fetching the row.  Reading anything else off such a proxy resolves it, and a proxy
                # whose row is gone raises rather than answers: that is not a usable user either.
                user.pk is not None
                and not user.is_anonymous
                and bool(getattr(user, "is_active", True))
            )
        except (ValueError, AttributeError):
            return False


@cache
def requires_logged_user(component: type[HtmxComponent]) -> bool:
    """Whether `component` declares a `user` that cannot be `None`.

    Annotating the field with a user model instead (the `user: Annotated[User, Field(exclude=True)]`
    base-component idiom) declares that the component is meaningless without a logged-in user, and
    djhtmx enforces it: building it without one raises `LoginRequired`, which the request paths turn
    into a trip to the login page.  Nothing else needs to be written; the annotation *is* the guard.

    """
    annotation = component.model_fields["user"].annotation
    if annotation is None or annotation is Any:
        return False
    elif get_origin(annotation) in (Union, types.UnionType):
        return types.NoneType not in get_args(annotation)
    else:
        return True


def _find_properties(cls: type[HtmxComponent]) -> frozenset[str]:
    return frozenset(
        attr
        for attr in dir(cls)
        if not attr.startswith("_")
        if attr not in PYDANTIC_MODEL_METHODS
        if isinstance(getattr(cls, attr), property | cached_property)
    )


def _validate_call_lazily(function: types.FunctionType) -> Callable[..., Any]:
    """Return `function` wrapped by `validate_call`:func:, which pydantic builds on the first call.

    `validate_call`:func: builds a pydantic schema for the parameters of `function` immediately.
    That takes about half a millisecond for each handler; an application with thousands of
    handlers loses seconds at startup.  The wrapper builds it on the first call instead, and keeps
    it.

    The first call can come from two threads at the same time: Django runs synchronous code in a
    pool of threads (``sync_to_async``, and the threads of a WSGI server), so two requests can call
    the same handler for the first time together.  A lock makes sure that only one thread builds
    the validator, and the other one uses it.  Without the lock, both threads build an identical
    validator and one is discarded: the result is the same, the lock only prevents the waste.

    The wrapper has the `raw_function` attribute of the wrapper of pydantic, so the code that looks
    through the wrapper of pydantic also looks through this one.  An annotation that pydantic
    cannot handle raises on the first call, not when the class is created.

    """
    validated: Callable[..., Any] | None = None

    @wraps(function)
    def wrapper(*args, **kwargs):
        nonlocal validated
        if validated is None:
            with _VALIDATE_CALL_LOCK:
                if validated is None:
                    validated = validate_call(config=_VALIDATE_CALL_CONFIG)(function)
        return validated(*args, **kwargs)

    wrapper.raw_function = function  # type: ignore[attr-defined]
    return wrapper


def _declares_parameters(function: types.FunctionType) -> bool:
    """Tell whether `function` has a parameter other than ``self``.

    It gives the same answer as `get_function_parameters`:func:, but it reads the code object.  That
    is much faster than `inspect.signature`:func:.  A function with a `__wrapped__` or a
    `__signature__` can show a signature that is not the one of its code, so it takes the slow path.

    """
    if hasattr(function, "__wrapped__") or hasattr(function, "__signature__"):
        return bool(get_function_parameters(function))
    else:
        code = function.__code__
        count = (
            code.co_argcount
            + code.co_kwonlyargcount
            + bool(code.co_flags & inspect.CO_VARARGS)
            + bool(code.co_flags & inspect.CO_VARKEYWORDS)
        )
        return any(name != "self" for name in code.co_varnames[:count])


def _get_own_field_hints(cls: type) -> dict[str, Any]:
    """Return the resolved annotations of the fields that `cls` declares itself.

    The result is what `get_type_hints(cls, include_extras=True)`:func: gives for these names, but
    it does not resolve the annotations of the base classes.  Names that start with ``_`` are not
    fields, so they are not in the result.

    """
    own = {
        name: annotation
        for name, annotation in cls.__annotations__.items()
        if not name.startswith("_")
    }
    if own:
        # A class with only these annotations resolves them in the same namespaces as `cls`.  When
        # it gets no namespaces, `get_type_hints` uses the class namespace as the globals and the
        # module namespace as the locals; these arguments keep that order.
        probe = type(cls.__name__, (), {"__annotations__": own, "__module__": cls.__module__})
        return get_type_hints(
            probe,
            globalns=dict(vars(cls)),
            localns=getattr(sys.modules.get(cls.__module__), "__dict__", {}),
            include_extras=True,
        )
    else:
        return {}


def _compose[**P, A, B](f: Callable[P, A], g: Callable[[A], B]) -> Callable[P, B]:
    def result(*args: P.args, **kwargs: P.kwargs):
        return g(f(*args, **kwargs))

    return result


def is_async_handler(handler) -> bool:
    """Tell whether `handler` is an ``async def``, with or without a ``yield`` in its body.

    Both shapes are refused when a component registers: the dispatcher calls a handler and consumes
    what comes back synchronously, so an ``async def`` handler would only ever hand it a coroutine
    or an async generator, and its body would never run.

    A wrapped handler is answered for as written: `validate_call`:func: marks the wrapper it puts
    around a coroutine function, but the wrapper around an async generator is an ordinary function,
    so the question has to reach the handler underneath it.

    """
    handler = _undecorated(handler)
    return iscoroutinefunction(handler) or isasyncgenfunction(handler)


def get_handler_kind(handler) -> HandlerKind:
    """Report which of the `HandlerKind`:obj: shapes `handler` has.

    The `validate_call`:func: wrapper djhtmx installs on every handler that declares parameters is
    an ordinary function, and would make each handler it wraps report as a plain ``function``; it is
    looked through, so the answer is about the handler as written.  A handler decorated with
    anything else answers for the decorator, which is the honest answer: another decorator need not
    have the shape of what it wraps.

    Raise `TypeError`:class: for an ``async def`` handler, which has no `HandlerKind`:obj: at all.
    A caller that can name the component declaring it should ask `is_async_handler`:func: first and
    raise the message that names it.

    """
    handler = _undecorated(handler)
    if is_async_handler(handler):
        raise TypeError(f"{handler!r} is an async handler, which has no handler kind")
    elif isgeneratorfunction(handler):
        return "generator"
    else:
        return "function"


def _undecorated(handler):
    """Return the function `handler` was written as, looking through `validate_call`:func:.

    Only that wrapper is looked through: it is the one djhtmx installs itself, and it delegates to
    the handler unchanged, so questions about the handler's shape are questions about the wrapped
    function.  Anything else `handler` may be decorated with is left in place.

    The result is to ask questions about, not to call: given a bound method it is the underlying
    function, without the instance.

    """
    return getattr(handler, "raw_function", handler)


logger = logging.getLogger(__name__)
_VALIDATE_CALL_CONFIG = ConfigDict(arbitrary_types_allowed=True)
# One lock for all the handlers: it is held only while a handler builds its validator, once in
# the life of the process.  It is reentrant, so a thread that already holds it never blocks on
# itself if a build ever reaches the first call of another handler.
_VALIDATE_CALL_LOCK = threading.RLock()
_ABSTRACT_BASE_REGEX = re.compile(r"^(_)?(Base|Abstract)[A-Z0-9_]")


class SSEEventRouter(HtmxComponent):
    _template_name = "htmx/SSEEventRouter.html"
    id: str = "djhtmx-sse-router"
