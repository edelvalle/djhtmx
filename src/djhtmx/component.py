from __future__ import annotations

import logging
import re
import typing as t
from collections import defaultdict
from dataclasses import dataclass, field as dataclass_field
from functools import cache, cached_property
from itertools import chain
from os.path import basename
from urllib.parse import urlparse

from django.conf import settings
from django.contrib.auth.models import AbstractUser, AnonymousUser
from django.core.signing import Signer
from django.db import models
from django.db.models.signals import post_save, pre_delete
from django.dispatch.dispatcher import receiver
from django.http import HttpRequest, HttpResponse, QueryDict
from django.shortcuts import resolve_url
from django.template import Context, loader
from django.utils.html import format_html
from django.utils.safestring import SafeString, mark_safe
from pydantic import BaseModel, ConfigDict, Field, validate_call
from pydantic.fields import ModelPrivateAttr
from typing_extensions import deprecated

from . import json
from .introspection import (
    annotate_model,
    filter_parameters,
    get_event_handler_event_types,
    get_function_parameters,
    get_related_fields,
    parse_request_data,
)
from .query import Query, QueryPatcher
from .tracing import sentry_span
from .utils import db, generate_id, get_model_subscriptions

__all__ = ("Component", "PydanticComponent", "Query", "ComponentNotFound")


class ComponentNotFound(LookupError):
    pass


RenderFunction = t.Callable[[Context | dict[str, t.Any] | None], SafeString]

PYDANTIC_MODEL_METHODS = {
    attr_name for attr_name in dir(BaseModel) if not attr_name.startswith("_")
}

_MAX_GENERATION = 50


def get_params(url: QueryDict | str | None) -> QueryDict:
    if isinstance(url, QueryDict):
        qd = QueryDict(None, mutable=True)
        qd.update(url)  # type: ignore
        return qd
    else:
        return QueryDict(
            query_string=urlparse(url).query if url else None,
            mutable=True,
        )


PyComp = t.TypeVar("PyComp", bound="PydanticComponent")


signer = Signer()


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
    oob: (
        tuple[
            t.Literal["beforebegin", "afterbegin", "beforeend", "afterend", "outerHTML"],
            str,
        ]
        | None
    ) = None

    @property
    def component_id(self):
        if isinstance(self.component, tuple):
            _, state = self.component
            return state.get("id")
        else:
            return self.component.id

    @classmethod
    def append(cls, target_: str, component_: type[PydanticComponent], **state):
        return cls(component=(component_, state), template=None, oob=("beforeend", target_))

    @classmethod
    def prepend(cls, target_: str, component_: type[PydanticComponent], **state):
        return cls(component=(component_, state), template=None, oob=("afterbegin", target_))

    @classmethod
    def after(cls, target_: str, component_: type["PydanticComponent"], **state):
        return cls(component=(component_, state), template=None, oob=("afterend", target_))

    @classmethod
    def before(cls, target_: str, component_: type[PydanticComponent], **state):
        return cls(component=(component_, state), template=None, oob=("beforeend", target_))

    @classmethod
    def update(cls, component: type[PydanticComponent], **state):
        return cls(component=(component, state))


@dataclass(slots=True)
class SendHtml:
    content: SafeString

    # XXX: Just to debug...
    debug_trace: str | None = None


@dataclass(slots=True)
class Emit:
    event: t.Any


@dataclass(slots=True)
class Signal:
    name: str


@dataclass(slots=True)
class SendState:
    component_id: str
    state: str
    command: t.Literal["send_state"] = "send_state"


@dataclass(slots=True)
class PushURL:
    url: str
    command: t.Literal["push_url"] = "push_url"

    @classmethod
    def from_params(cls, params: QueryDict):
        return cls("?" + params.urlencode())


Command = Destroy | Redirect | Focus | DispatchEvent | SkipRender | Render | Emit | Signal | Execute
ProcessedCommand = Destroy | Redirect | Focus | DispatchEvent | SendHtml | SendState | PushURL


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
        request: HttpRequest,
    ) -> Repository:
        """Get or build the Repository from the request.

        If the request has already a Repository attached, return it without
        further processing.

        Otherwise, build the repository from the request's POST and attach it
        to the request.

        """
        if (result := getattr(request, "djhtmx", None)) is None:
            result = cls(
                user=getattr(request, "user", AnonymousUser()),
                params=get_params(request.GET),
            )
            setattr(request, "djhtmx", result)
        return result

    @classmethod
    def from_websocket(
        cls,
        user: AbstractUser | AnonymousUser,
        states: list[str],
        subscriptions: dict[str, str],
    ):
        return cls(
            user=user,
            params=get_params(None),
        )

    @staticmethod
    def load_states_by_id(states: list[str]) -> dict[str, dict[str, t.Any]]:
        return {
            state["id"]: state for state in [json.loads(signer.unsign(state)) for state in states]
        }

    @staticmethod
    def load_subscriptions(
        states_by_id: dict[str, dict[str, t.Any]], subscriptions: dict[str, str]
    ) -> dict[str, set[str]]:
        subscriptions_to_ids: dict[str, set[str]] = defaultdict(set)
        for component_id, component_subscriptions in subscriptions.items():
            # Register query string subscriptions
            component_name = states_by_id[component_id]["hx_name"]
            for patcher in _get_query_patchers(component_name):
                subscriptions_to_ids[patcher.signal_name].add(component_id)

            # Register other subscriptions
            for subscription in component_subscriptions.split(","):
                subscriptions_to_ids[subscription].add(component_id)
        return subscriptions_to_ids

    def __init__(
        self,
        user: AbstractUser | AnonymousUser,
        params: QueryDict,
    ):
        self.user = user
        self.component_by_id: dict[str, PydanticComponent] = {}
        self.states_by_id: dict[str, dict[str, t.Any]] = {}
        self.subscriptions: dict[str, set[str]] = defaultdict(set)
        self.params = params

    # Component life cycle & management

    def add(self, states: list[str], subscriptions: dict[str, str]):
        self.states_by_id |= self.load_states_by_id(states)
        self.subscriptions.update(self.load_subscriptions(self.states_by_id, subscriptions))

    def register_component(self, component: PyComp) -> PyComp:
        self.component_by_id[component.id] = component
        return component

    def unregister_component(self, component_id: str):
        self.states_by_id.pop(component_id, None)
        self.component_by_id.pop(component_id, None)
        for subscription, component_ids in self.subscriptions.items():
            self.subscriptions[subscription].difference_update([component_id])

    async def dispatch_event(
        self,
        component_id: str,
        event_handler: str,
        event_data: dict[str, t.Any],
    ) -> t.AsyncIterable[ProcessedCommand]:
        commands: list[Command] = [Execute(component_id, event_handler, event_data)]

        # Listen to model signals during execution
        @receiver(post_save, weak=True)
        @receiver(pre_delete, weak=True)
        def _listen_to_post_save_and_pre_delete(
            sender: type[models.Model],
            instance: models.Model,
            created: bool = None,
            **kwargs,
        ):
            if created is None:
                action = "deleted"
            elif created:
                action = "created"
            else:
                action = "updated"

            signals = get_model_subscriptions(instance, actions=(action,))
            for field in get_related_fields(sender):
                fk_id = getattr(instance, field.name)
                signal = f"{field.related_model_name}.{fk_id}.{field.relation_name}"
                signals.update((signal, f"{signal}.{action}"))

            commands.extend([Signal(name) for name in signals])

        # Keeps track of destroyed components to avoid rendering them
        destroyed_ids: set[str] = set()
        sent_html = set()

        # Command loop
        while commands:
            print()
            command = commands.pop(0)
            await db(print)(command)
            match command:
                case Execute(component_id, event_handler, event_data):
                    # handle event
                    component = await db(self.get_component_by_id)(component_id)
                    handler = getattr(component, event_handler)
                    handler_kwargs = filter_parameters(handler, parse_request_data(event_data))
                    component_was_rendered = False
                    if emited_commands := await db(handler)(**handler_kwargs):
                        for command in await db(list)(emited_commands):
                            component_was_rendered = (
                                component_was_rendered
                                or isinstance(command, SkipRender)
                                or isinstance(command, Render)
                                and command.component_id == component.id
                            )
                            commands.append(command)

                    if not component_was_rendered:
                        commands.append(Render(component))

                    if signals := self.update_params_from(component):
                        yield PushURL.from_params(self.params)
                        commands.extend(Signal(s) for s in signals)

                case SkipRender(component):
                    yield SendState(
                        component_id=component.id,
                        state=signer.sign(component.model_dump_json()),
                    )

                case Render(component, template, oob) as command:
                    # do not render destroyed components, skip
                    if command.component_id in destroyed_ids:
                        continue

                    # instantiate the component
                    if isinstance(component, tuple):
                        component_type, state = component
                        component = await db(self.build)(component_type.__name__, state)

                    # why? because this is a partial render and the state of the object is not
                    # updated in the root tag, and it has to be up to date in case of disconnection
                    if template:
                        commands.append(SkipRender(component))

                    html = await db(self.render_html)(component, oob=oob, template=template)
                    if html not in sent_html:
                        yield SendHtml(html, debug_trace=f"{component.hx_name}({component.id})")
                        sent_html.add(html)

                case Destroy(component_id) as command:
                    destroyed_ids.add(component_id)
                    self.unregister_component(component_id)
                    yield command

                case Emit(event):
                    for component in await db(self.get_components_by_names)(LISTENERS[type(event)]):
                        logger.debug("< AWAKED: %s id=%s", component.hx_name, component.id)
                        if emited_commands := await db(component._handle_event)(event):  # type: ignore
                            commands.extend(await db(list)(emited_commands))
                        commands.append(Render(component))

                        if signals := self.update_params_from(component):
                            yield PushURL.from_params(self.params)
                            commands.extend(Signal(s) for s in signals)

                case Signal(signal):
                    for component in await db(self.get_components_subscribed_to)(signal):
                        commands.append(Render(component))

                case Redirect(_) | Focus(_) | DispatchEvent(_) as command:
                    yield command

    def get_components_subscribed_to(self, signal: str) -> t.Iterable[PydanticComponent]:
        component_ids = list(self.subscriptions[signal])
        component_ids.extend(
            component.id
            for component in self.component_by_id.values()
            if component._match_subscription(signal)
        )
        return [self.get_component_by_id(c_id) for c_id in sorted(component_ids)]

    def update_params_from(self, component: PydanticComponent) -> set[str]:
        """Updates self.params based on the state of the component

        Return the set of signals that should be triggered as the result of
        the update.

        """
        updated_params: set[str] = set()
        if patchers := _get_query_patchers(component.hx_name):
            for patcher in patchers:
                updated_params.update(
                    patcher.get_updates_for_params(
                        getattr(component, patcher.field_name, None),
                        self.params,
                    )
                )
        return updated_params

    def get_component_by_id(self, component_id: str):
        """Return (possibly build) the component by its ID.

        If the component was already built, get it unchanged, otherwise build
        it from the request's payload and return it.

        If the `component_id` cannot be found, raise a KeyError.

        """
        if state := self.states_by_id.get(component_id):
            name = state["hx_name"]
        else:
            name = self.component_by_id[component_id].hx_name
        return self.build(name, {"id": component_id})

    def build(self, component_name: str, state: dict[str, t.Any]):
        """Build (or update) a component's state."""
        # Take state from stored state
        if component_id := state.pop("id", None):
            state = self.states_by_id.pop(component_id, {}) | state

            # Remove from the static subscriptions
            for subscription, component_ids in self.subscriptions.items():
                self.subscriptions[subscription].difference_update([component_id])

        # Patch it with whatever is the the GET params if needed
        for patcher in _get_query_patchers(component_name):
            state |= patcher.get_update_for_state(self.params)

        # Build
        if component_id and (component := self.component_by_id.get(component_id)):
            if state:
                # some state was passed to the component, so it has to be updated
                component = component.model_validate(
                    component.model_dump() | state | {"user": self.user, "id": component_id}
                )
        else:
            kwargs = (
                state
                | {"hx_name": component_name, "user": self.user}
                | ({"id": component_id} if component_id else {})
            )
            component = REGISTRY[component_name](**kwargs)

        return self.register_component(component)

    def get_components_by_names(self, names: t.Iterable[str]) -> t.Iterable[PydanticComponent]:
        # go over awaken components
        components = []
        for name in names:
            for component in self.component_by_id.values():
                if component.hx_name == name:
                    components.append(self.build(component.hx_name, {"id": component.id}))

            # go over asleep components
            for state in list(self.states_by_id.values()):
                if state["hx_name"] == name:
                    components.append(self.build(name, state))
        return sorted(components, key=lambda c: c.id)

    def render_html(
        self,
        component: PydanticComponent,
        oob: tuple[str, str] = None,
        template: str = None,
    ) -> SafeString:
        where, target = oob if oob else (None, None)
        html = [
            format_html('<div hx-swap-oob="{where}: {target}">', where=where, target=target)
            if oob
            else "",
            component._get_template(template)(
                component._get_context() | {"htmx_repo": self},
            ),
            "</div>" if oob else "",
        ]
        return mark_safe("".join(filter(None, html)))


REGISTRY: dict[str, type[PydanticComponent]] = {}
LISTENERS: dict[type, set[str]] = defaultdict(set)
FQN: dict[type[PydanticComponent], str] = {}
RENDER_FUNC: dict[str, RenderFunction] = {}


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
    user: t.Annotated[AnonymousUser | AbstractUser, Field(exclude=True)]
    hx_name: str

    @property
    def subscriptions(self) -> set[str]:
        return set()

    def _match_subscription(self, signal: str):
        return (
            signal in _get_querystring_subscriptions(self.hx_name) or signal in self.subscriptions
        )

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
