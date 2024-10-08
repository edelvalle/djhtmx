from __future__ import annotations

import logging
import typing as t
from collections import defaultdict
from dataclasses import dataclass, field as Field

from django.contrib.auth.models import AbstractBaseUser, AnonymousUser
from django.core.signing import Signer
from django.db import models
from django.db.models.signals import post_save, pre_delete
from django.dispatch.dispatcher import receiver
from django.http import HttpRequest, QueryDict
from django.utils.html import format_html
from django.utils.safestring import SafeString, mark_safe
from uuid6 import uuid7

from . import json
from .command_queue import CommandQueue
from .component import (
    LISTENERS,
    REGISTRY,
    BuildAndRender,
    Command,
    ComponentNotFound,
    Destroy,
    DispatchDOMEvent,
    Emit,
    Execute,
    Focus,
    PydanticComponent,
    Redirect,
    Render,
    Signal,
    SkipRender,
    _get_query_patchers,
)
from .introspection import Unset, filter_parameters, get_related_fields
from .settings import conn
from .utils import db, get_model_subscriptions, get_params

signer = Signer()

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SendHtml:
    content: SafeString

    # XXX: Just to debug...
    debug_trace: str | None = None


@dataclass(slots=True)
class PushURL:
    url: str
    command: t.Literal["push_url"] = "push_url"

    @classmethod
    def from_params(cls, params: QueryDict):
        return cls("?" + params.urlencode())


ProcessedCommand = Destroy | Redirect | Focus | DispatchDOMEvent | SendHtml | PushURL


class Repository:
    """An in-memory (cheap) mapping of component IDs to its states.

    When an HTMX request comes, all the state from all the components are
    placed in a registry.  This way we can instantiate components if/when
    needed.

    For instance, if a component is subscribed to an event and the event fires
    during the request, that component is rendered.

    """

    @staticmethod
    def new_session_id():
        return f"djhtmx:{uuid7().hex}"

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
            if signed_session := request.META.get("HTTP_HX_SESSION"):
                session_id = signer.unsign(signed_session)
                reset_session = bool(request.META.get("HTTP_HX_BOOSTED"))
            else:
                session_id = cls.new_session_id()
                reset_session = True

            session = Session(session_id)
            if reset_session:
                session.reset()

            result = cls(
                user=getattr(request, "user", AnonymousUser()),
                session=session,
                params=get_params(request),
            )
            setattr(request, "djhtmx", result)
        return result

    @classmethod
    def from_websocket(
        cls,
        user: AbstractBaseUser | AnonymousUser,
    ):
        return cls(
            user=user,
            session=Session(cls.new_session_id()),  # TODO: take the session from the websocket url
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
        user: AbstractBaseUser | AnonymousUser,
        session: Session,
        params: QueryDict,
    ):
        self.user = user
        self.session = session
        self.params = params

    # Component life cycle & management

    def unregister_component(self, component_id: str):
        # delete component state
        self.session.unregister_component(component_id)

    async def adispatch_event(
        self,
        component_id: str,
        event_handler: str,
        event_data: dict[str, t.Any],
    ) -> t.AsyncIterable[ProcessedCommand]:
        commands = CommandQueue([Execute(component_id, event_handler, event_data)])

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

        # Command loop
        while commands:
            processed_commands = self._run_command(commands)
            while command := await db(next)(processed_commands, None):
                yield command

    def dispatch_event(
        self,
        component_id: str,
        event_handler: str,
        event_data: dict[str, t.Any],
    ) -> t.Iterable[ProcessedCommand]:
        commands = CommandQueue([Execute(component_id, event_handler, event_data)])

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

        # Command loop
        while commands:
            for command in self._run_command(commands):
                yield command

    def _run_command(self, commands: CommandQueue) -> t.Generator[ProcessedCommand, None, None]:
        command = commands.pop()
        logger.debug("COMMAND: %s", command)
        match command:
            case Execute(component_id, event_handler, event_data):
                # handle event
                component = self.get_component_by_id(component_id)
                handler = getattr(component, event_handler)
                handler_kwargs = filter_parameters(handler, event_data)
                emited_commands = handler(**handler_kwargs)
                yield from self._process_emited_commands(component, emited_commands, commands)

            case SkipRender(component):
                self.session.store(component)

            case BuildAndRender(component_type, state, oob):
                component = self.build(component_type.__name__, state)
                commands.append(Render(component, oob=oob))

            case Render(component, template, oob):
                html = self.render_html(component, oob=oob, template=template)
                yield SendHtml(
                    html,
                    debug_trace=f"{component.hx_name}({component.id})",
                )

            case Destroy(component_id) as command:
                self.unregister_component(component_id)
                yield command

            case Emit(event):
                for component in self.get_components_by_names(LISTENERS[type(event)]):
                    logger.debug("< AWAKED: %s id=%s", component.hx_name, component.id)
                    emited_commands = component._handle_event(event)  # type: ignore
                    yield from self._process_emited_commands(component, emited_commands, commands)

            case Signal(signal):
                for component in self.get_components_subscribed_to(signal):
                    logger.debug("< AWAKED: %s id=%s", component.hx_name, component.id)
                    commands.append(Render(component))

            case Redirect(_) | Focus(_) | DispatchDOMEvent(_) as command:
                yield command

    def _process_emited_commands(
        self,
        component: PydanticComponent,
        emmited_commands: t.Iterable[Command] | None,
        commands: CommandQueue,
    ) -> t.Iterable[ProcessedCommand]:
        component_was_rendered = False
        for command in emmited_commands or []:
            component_was_rendered = component_was_rendered or (
                isinstance(command, (SkipRender, Render)) and command.component.id == component.id
            )
            commands.append(command)

        if not component_was_rendered:
            commands.append(Render(component))

        if signals := self.update_params_from(component):
            yield PushURL.from_params(self.params)
            commands.extend(Signal(s) for s in signals)

    def get_components_subscribed_to(self, signal: str) -> t.Iterable[PydanticComponent]:
        return (
            self.get_component_by_id(c_id)
            for c_id in sorted(self.session.get_component_ids_subscribed_to(signal))
        )

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
        if state := self.session.get_state(component_id):
            return self.build(state["hx_name"], state, retrieve_state=False)
        else:
            raise ComponentNotFound(f"Component with id {component_id} not found")

    def build(self, component_name: str, state: dict[str, t.Any], retrieve_state: bool = True):
        """Build (or update) a component's state."""

        # Retrieve state from storage
        if retrieve_state and (component_id := state.get("id")):
            state = (self.session.get_state(component_id) or {}) | state

        # Patch it with whatever is the the GET params if needed
        for patcher in _get_query_patchers(component_name):
            state |= patcher.get_update_for_state(self.params)

        # Inject component name and user
        kwargs = state | {"hx_name": component_name, "user": self.user}

        return REGISTRY[component_name](**kwargs)

    def get_components_by_names(self, names: t.Iterable[str]) -> t.Iterable[PydanticComponent]:
        # go over awaken components
        for name in names:
            for state in self.session.get_all_states():
                if state["hx_name"] == name:
                    yield self.build(name, {"id": state["id"]})

    def render_html(
        self,
        component: PydanticComponent,
        oob: str = None,
        template: str = None,
    ) -> SafeString:
        # rendering the component means that it will be sent to the UI, the state has to be stored
        self.session.store(component)

        html = mark_safe(
            component._get_template(template)(
                component._get_context() | {"htmx_repo": self, "hx_oob": oob == "true"}
            ).strip()
        )
        if oob and oob != "true":
            html = mark_safe(
                "".join([format_html('<div hx-swap-oob="{oob}">', oob=oob), html, "</div>"])
            )
        return html


@dataclass(slots=True)
class Session:
    id: str
    cache: dict[str, dict[str, t.Any] | None] = Field(default_factory=dict)

    def reset(self):
        if keys := conn.keys(f"{self.id}:*"):
            conn.delete(*keys)  # type: ignore

    def unregister_component(self, component_id: str):
        with conn.pipeline() as pipe:
            # delete state
            pipe.hdel(f"{self.id}:states", component_id)
            # delete from subscriptions
            for key in conn.keys(f"{self.id}:subs:*"):  # type: ignore
                pipe.srem(key, component_id)
            pipe.execute()

    def get_state(self, component_id: str) -> dict[str, t.Any] | None:
        if (state := self.cache.get(component_id, Unset)) is not Unset:
            return state  # type: ignore
        elif state := conn.hget(f"{self.id}:states", component_id):
            state = json.loads(state)  # type: ignore
            self.cache[component_id] = state
            return state
        else:
            return None

    def get_component_ids_subscribed_to(self, signal: str) -> list[str]:
        _, keys = conn.sscan(f"{self.id}:subs:{signal}")  # type: ignore
        return [k.decode() for k in keys]

    def get_all_states(self) -> t.Iterable[dict[str, t.Any]]:
        for component_id, state in conn.hgetall(f"{self.id}:states").items():  # type: ignore
            state = self.cache[str(component_id)] = json.loads(state)
            yield state

    def store(self, component: PydanticComponent):
        self.cache[component.id] = component.model_dump()
        with conn.pipeline() as pipe:
            pipe.hset(f"{self.id}:states", component.id, component.model_dump_json())
            for signal in component._get_all_subscriptions():
                pipe.sadd(f"{self.id}:subs:{signal}", component.id)
            pipe.execute()

    def set_ttl(self, ttl: int = 3600):
        with conn.pipeline() as pipe:
            for key in conn.keys(f"{self.id}:*"):  # type: ignore
                pipe.expire(key, ttl)
            pipe.execute()

    def __del__(self):
        self.set_ttl()
