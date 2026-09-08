from __future__ import annotations

import logging
import random
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import field as Field
from typing import Any

from django.core.signing import Signer
from django.http import HttpRequest, QueryDict
from django.utils.html import format_html
from django.utils.safestring import SafeString, mark_safe
from pydantic import ValidationError
from uuid6 import uuid7

from djhtmx.tracing import tracing_span

from . import json
from .commands import (
    Destroy,
    Execute,
    ProcessedCommand,
)
from .component import (
    REGISTRY,
    HtmxComponent,
    _get_query_patchers,
)
from .exceptions import LoginRequired
from .settings import (
    KEY_SIZE_ERROR_THRESHOLD,
    KEY_SIZE_SAMPLE_PROB,
    KEY_SIZE_WARN_THRESHOLD,
    SESSION_TTL,
    conn,
)
from .utils import compact_hash, get_fqn, get_params

signer = Signer()

logger = logging.getLogger(__name__)

# Sentinel distinguishing "user not provided" from an explicit `user=None`.
_UNSET: Any = object()


# `ProcessedCommand` is re-exported from `.commands` so existing imports
# (`from djhtmx.repo import ProcessedCommand`) keep working.
__all__ = ("ProcessedCommand", "Repository", "Session", "signer")


class Repository:
    """An in-memory (cheap) mapping of component IDs to its states.

    When an HTMX request comes, all the state from all the components are
    placed in a registry.  This way we can instantiate components if/when
    needed.

    For instance, if a component is subscribed to an event and the event fires
    during the request, that component is rendered.

    The repository is synchronous throughout: it is built and driven on a
    sync-work pool thread (see `command_processor` and `sse_executor`), so every
    ORM touch — Model-field resolution, the `request.user` evaluation, the
    template render — happens on the thread that owns the DB connection.

    """

    @staticmethod
    def new_session_id():
        return f"djhtmx:{uuid7().hex}"

    @classmethod
    def from_request(
        cls,
        request: HttpRequest,
        *,
        user: Any = _UNSET,
    ) -> Repository:
        """Get or build the Repository from the request.

        If the request has already a Repository attached, return it without
        further processing.

        Otherwise, build the repository from the request's POST and attach it
        to the request.

        `user` may be passed explicitly; otherwise it falls back to the lazy
        `request.user`.  The lazy proxy is resolved later, when the dispatch
        evaluates it on the pool thread (never on the event loop), so building
        the repository itself triggers no DB hit.

        """
        from django.contrib.auth.models import AnonymousUser

        if (result := getattr(request, "htmx_repo", None)) is None:
            if (signed_session := request.META.get("HTTP_HX_SESSION")) and not bool(
                request.META.get("HTTP_HX_BOOSTED")
            ):
                session_id = signer.unsign(signed_session)
            else:
                session_id = cls.new_session_id()

            session = Session(session_id)

            result = cls(
                user=getattr(request, "user", AnonymousUser()) if user is _UNSET else user,
                session=session,
                params=get_params(request),
            )
            request.htmx_repo = result  # type: ignore
        return result

    @classmethod
    def from_websocket(cls, user):
        return cls(
            user=user,
            session=Session(cls.new_session_id()),  # TODO: take the session from the websocket url
            params=get_params(None),
        )

    @staticmethod
    def load_states_by_id(states: list[str]) -> dict[str, dict[str, Any]]:
        return {
            state["id"]: state for state in [json.loads(signer.unsign(state)) for state in states]
        }

    @staticmethod
    def load_subscriptions(
        states_by_id: dict[str, dict[str, Any]], subscriptions: dict[str, str]
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

    def __init__(self, user, session: Session, params: QueryDict):
        self.user = user
        self.session = session
        self.session_signed_id = signer.sign(session.id)
        self.session_hash = compact_hash(session.id)
        self.params = params

    def unregister_component(self, component_id: str):
        # Delete component state recursively, then clean up the SSE consumer
        # record for every component that was just destroyed (the explicit one
        # plus any children cascaded by `Session.unregister_component`).
        from .sse import unregister_consumer

        before = set(self.session.unregistered)
        self.session.unregister_component(component_id)  # in-memory
        for id_ in self.session.unregistered - before:
            unregister_consumer(self.session.id, id_)

    def dispatch_event(
        self,
        component_id: str,
        event_handler: str,
        event_data: dict[str, Any],
    ) -> Iterable[ProcessedCommand]:
        from .command_processor import CommandProcessor

        yield from CommandProcessor(self).process([
            Execute(component_id, event_handler, event_data)
        ])

    def update_params_from(self, component: HtmxComponent) -> set[str]:
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

    def get_component_by_id(self, component_id: str) -> Destroy | HtmxComponent:
        """Return (possibly build) the component by its ID.

        If the component was already built, get it unchanged, otherwise build
        it from the request's payload and return it.

        If the `component_id` cannot be found, return a `Destroy`.

        """
        if state := self.session.get_state(component_id):
            return self.build(state["hx_name"], state, retrieve_state=False)
        else:
            logger.error(
                "Component with id %s not found in session %s", component_id, self.session.id
            )
            return Destroy(component_id)

    def get_components_subscribed_to(
        self, signals: set[tuple[str, str]]
    ) -> Iterable[HtmxComponent | Destroy]:
        for c_id in sorted(self.session.get_component_ids_subscribed_to(signals)):
            yield self.get_component_by_id(c_id)

    def build(
        self,
        component_name: str,
        state: dict[str, Any],
        retrieve_state: bool = True,
        parent_id: str | None = None,
    ):
        """Build (or update) a component's state.

        Model-typed fields are resolved (pk -> instance) by their field
        validator during construction, with the sync ORM on the calling pool
        thread; lazy Model fields defer that query to first access.
        """
        if retrieve_state and (component_id := state.get("id")):
            state = (self.session.get_state(component_id) or {}) | state
        state = self._apply_query_patchers(component_name, state)
        return self._construct(component_name, state, parent_id)

    def _apply_query_patchers(self, component_name: str, state: dict[str, Any]) -> dict[str, Any]:
        """Overlay query-string values onto the state (pure CPU, no I/O).

        Model query fields carry a pk; the instance is resolved afterwards by
        the field validator during construction.
        """
        for patcher in _get_query_patchers(component_name):
            state |= patcher.get_update_for_state(self.params)
        return state

    def _construct(self, component_name: str, state: dict[str, Any], parent_id: str | None):
        """Construct the pydantic component from its state dict.

        Model-field validators resolve pk -> instance here (sync ORM), and the
        lazy `self.user` is evaluated here too — all on the pool thread that owns
        the connection, never on the event loop.
        """
        from django.contrib.auth.models import AnonymousUser

        with tracing_span("Repository.build", component_name=component_name):
            kwargs = state | {
                "hx_name": component_name,
                "session_id": self.session.id,
                "user": None if isinstance(self.user, AnonymousUser) else self.user,
            }
            component_class = REGISTRY[component_name]
            try:
                component = component_class(**kwargs)  # type: ignore[arg-type]
            except ValidationError as error:
                # Every way of rejecting the user leaves here as one exception, so a caller never
                # has to tell them apart.  Pydantic's own type check reports `is_instance_of` for a
                # `None` user; resolving the row reports `value_error` when the primary key matches
                # nothing (a deleted account, or a state that outlived it); and applications raise
                # either shape from their own validators.  All of them mean the request has no user
                # to act as, which is what the transports turn into a trip to the login page.
                if any(
                    detail["type"] in ("is_instance_of", "value_error")
                    and detail["loc"] == ("user",)
                    for detail in error.errors()
                ):
                    raise LoginRequired(get_fqn(component_class)) from error
                else:
                    raise
            self.session.register_child(parent_id, component.id)
            return component

    def get_components_by_names(self, *names: str) -> Iterable[HtmxComponent]:
        # go over awaken components
        for name in names:
            for state in self.session.get_all_states():
                if state["hx_name"] == name:
                    yield self.build(name, {"id": state["id"]})

    def render_html(
        self,
        component: HtmxComponent,
        oob: str | None = None,
        template: str | None = None,
        lazy: bool | None = None,
        context: dict[str, Any] | None = None,
    ) -> SafeString:
        """Render a component to HTML and register its SSE consumer record."""
        self.session.store(component)
        from .sse import register_component

        register_component(self.session.id, component)
        return self._render_template(
            component, oob=oob, template=template, lazy=lazy, context=context
        )

    def _render_template(
        self,
        component: HtmxComponent,
        oob: str | None = None,
        template: str | None = None,
        lazy: bool | None = None,
        context: dict[str, Any] | None = None,
    ) -> SafeString:
        """Build the render context and render the template (ORM/CPU, no Redis)."""
        lazy = component.lazy if lazy is None else lazy
        with tracing_span(
            "Repository.render_html",
            component_name=component.hx_name,
            oob=str(oob),
            template=str(template),
            lazy=str(lazy),
        ):
            final_context = {
                "htmx_repo": self,
                "hx_oob": oob == "true",
                "this": component,
            }

            if lazy:
                template = template or component._template_name_lazy
                final_context |= {"hx_lazy": True} | component._get_lazy_context() | (context or {})
            else:
                final_context |= component._get_context() if context is None else context  # type: ignore[call-overload]

            html = mark_safe(component._get_template(template)(final_context).strip())

            # if performing some kind of append, the component has to be wrapped
            if oob and oob != "true":
                html = mark_safe(
                    "".join([
                        format_html('<div hx-swap-oob="{oob}">', oob=oob),
                        html,
                        "</div>",
                    ])
                )
            return html


@dataclass(slots=True)
class Session:
    id: str

    read: bool = False
    is_dirty: bool = False

    # dict[component_id -> state]
    states: dict[str, str] = Field(default_factory=dict)

    # dict[component_id -> set[signals]]
    subscriptions: defaultdict[str, set[str]] = Field(default_factory=lambda: defaultdict(set))

    # dict[parent_id -> set[child_ids]]
    children: defaultdict[str, set[str]] = Field(default_factory=lambda: defaultdict(set))

    # set[component_id]
    unregistered: set[str] = Field(default_factory=set)

    def store(self, component: HtmxComponent):
        state = component.model_dump_json()
        if self.states.get(component.id) != state:
            self.states[component.id] = state
            self.is_dirty = True

        subscriptions = component._get_all_subscriptions()
        if self.subscriptions[component.id] != subscriptions:
            self.subscriptions[component.id] = subscriptions
            self.is_dirty = True

    def unregister_component(self, component_id: str):
        # Recursively unregister all children first
        if child_ids := self.children.get(component_id):
            for child_id in child_ids.copy():  # Copy to avoid modification during iteration
                self.unregister_component(child_id)

        # Remove from parent's children list
        for child_ids in self.children.values():
            if component_id in child_ids:
                child_ids.remove(component_id)
                break

        # Remove this component's children mapping
        self.children.pop(component_id, None)

        # Remove component state and subscriptions
        self.states.pop(component_id, None)
        self.subscriptions.pop(component_id, None)
        self.unregistered.add(component_id)
        self.is_dirty = True

    def register_child(self, parent_id: str | None, child_id: str):
        """Register a parent-child relationship between components."""
        if parent_id and parent_id != child_id and child_id not in self.children[parent_id]:
            self.children[parent_id].add(child_id)
            self.is_dirty = True

    def get_state(self, component_id: str) -> dict[str, Any] | None:
        self._ensure_read()
        if state := self.states.get(component_id):
            return json.loads(state)
        else:
            return None

    def get_component_ids_subscribed_to(self, signals: set[tuple[str, str]]) -> Iterable[str]:
        self._ensure_read()
        yield from self._ids_subscribed_to(signals)

    def _ids_subscribed_to(self, signals: set[tuple[str, str]]) -> Iterable[str]:
        for component_id, subscribed_to in self.subscriptions.items():
            # here we ignore signals emitted by the component it self
            if subscribed_to.intersection(signal for signal, cid in signals if cid != component_id):
                yield component_id

    def get_all_states(self) -> Iterable[dict[str, Any]]:
        self._ensure_read()
        return [json.loads(state) for state in self.states.values()]

    def _apply_raw_states(self, raw: dict) -> None:
        """Populate the in-memory maps from a raw `{id: state}` Redis hash."""
        for component_id, state in raw.items():
            component_id = component_id.decode()
            if component_id == "__subs__":
                # dict[component_id -> list[signals]]
                for component_id, signals in json.loads(state).items():
                    self.subscriptions[component_id] = set(signals)
            elif component_id == "__children__":
                # dict[parent_id -> list[child_ids]]
                for parent_id, child_ids in json.loads(state).items():
                    self.children[parent_id] = set(child_ids)
            else:
                self.states[component_id] = state.decode()
        self.read = True

    def _ensure_read(self):
        if not self.read:
            self._apply_raw_states(conn.hgetall(f"{self.id}:states"))  # type: ignore

    def flush(self, ttl: int = SESSION_TTL):
        if self.is_dirty:
            key = f"{self.id}:states"
            # Apply the dirty hash and refresh its TTL in a single MULTI/EXEC so
            # a concurrent reader (`_ensure_read`) can never observe partial
            # state — e.g. updated `states` but a `__subs__`/`__children__` from
            # the previous flush.
            with conn.pipeline(transaction=True) as pipe:
                if self.unregistered:
                    pipe.hdel(key, *self.unregistered)
                if self.states:
                    pipe.hset(key, mapping=self.states)
                pipe.hset(key, "__subs__", json.dumps(self.subscriptions))
                pipe.hset(key, "__children__", json.dumps(self.children))
                pipe.expire(key, ttl)
                pipe.execute()
            self.unregistered.clear()
            # The command MEMORY USAGE is considered slow:
            # https://redis.io/docs/latest/commands/memory-usage/
            #
            # So we perform a trivial sampling with some prob to test the memory usage of the state.
            if random.random() <= KEY_SIZE_SAMPLE_PROB:
                self._check_key_size(conn.memory_usage(key))
            self.is_dirty = False

    def _check_key_size(self, usage: object) -> None:
        if isinstance(usage, int):
            if KEY_SIZE_ERROR_THRESHOLD and usage > KEY_SIZE_ERROR_THRESHOLD:
                logger.error(
                    "HTMX session's size (%s) exceeded the size threshold %s",
                    usage,
                    KEY_SIZE_ERROR_THRESHOLD,
                )
            elif KEY_SIZE_WARN_THRESHOLD and usage > KEY_SIZE_WARN_THRESHOLD:
                logger.warning(
                    "HTMX session's size (%s) exceeded the size threshold %s",
                    usage,
                    KEY_SIZE_WARN_THRESHOLD,
                )
