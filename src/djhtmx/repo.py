from __future__ import annotations

import logging
import random
from collections import defaultdict
from collections.abc import Iterable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from dataclasses import field as Field
from itertools import accumulate
from typing import TYPE_CHECKING, Any, Literal, assert_never, cast

from django.core.signing import Signer
from django.db import models
from django.db.models import Prefetch, prefetch_related_objects
from django.db.models.constants import LOOKUP_SEP
from django.http import HttpRequest, QueryDict
from django.utils.html import format_html
from django.utils.safestring import SafeString, mark_safe
from pydantic import ValidationError
from uuid6 import uuid7

from djhtmx.tracing import metric_incr, tracing_span

from . import json
from .commands import (
    Destroy,
    Execute,
    ProcessedCommand,
)
from .component import (
    REGISTRY,
    HandlerKind,
    HtmxComponent,
    _get_query_patchers,
)
from .exceptions import LoginRequired
from .introspection import ModelConfig, normalize_pk
from .settings import (
    DEFAULT_MODEL_CACHE,
    DISABLE_MODEL_CACHE,
    KEY_SIZE_ERROR_THRESHOLD,
    KEY_SIZE_SAMPLE_PROB,
    KEY_SIZE_WARN_THRESHOLD,
    SESSION_TTL,
    conn,
)
from .utils import atomic_if_requested, compact_hash, get_fqn, get_model_full_label, get_params

if TYPE_CHECKING:
    from .sse import SSESubscription

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
    def current() -> Repository | None:
        """Return the repository of the lifecycle running in this context, or None outside any.

        This is how code that cannot be handed the repository -- a pydantic validator, for one --
        reaches the render-cycle caches.  None is the normal answer for a component built outside
        any request, SSE wakeup or test dispatch, and callers must keep working without the caches.

        """
        match _current.get():
            case Repository() as repo:
                return repo
            case HttpRequest() as request:
                return getattr(request, "htmx_repo", None)
            case None:
                return None

    @staticmethod
    @contextmanager
    def activate(locator: Repository | HttpRequest) -> Iterator[None]:
        """Make `locator` what `current`:meth: answers with while the block runs.

        Pass the repository itself when it already exists.  Pass the request when the repository is
        built lazily inside the block, as a page render does through `from_request`:meth:; it is
        looked up on the request at each call of `current`:meth:.

        Activations nest, and each one restores the previous value on exit, so a worker thread
        reused by the next request never sees a stale repository.  The value travels with the
        `contextvars` context, into `sync_to_async` threads and the SSE render executor alike.

        """
        token = _current.set(locator)
        try:
            yield
        finally:
            _current.reset(token)

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
        # SSE keeps reverse indexes, (event type, topic) -> consumer, so a wakeup finds its
        # consumers without scanning the sessions; that is why it cannot ride the Session's single
        # entry holding every component's state.  Both writers of a component's SSE state -- the
        # consumer record and the root tag -- need the subscriptions, and this render-cycle cache
        # is what keeps them to one computation.
        self._sse_subscriptions: dict[int, tuple[HtmxComponent, set[SSESubscription]]] = {}
        # Nested by model, so invalidating a whole model drops one entry instead of scanning them all.
        self._model_instances: dict[type[models.Model], dict[object, _CachedInstance]] = {}

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

    def get_handler_kind(self, component_id: str, event_handler: str) -> HandlerKind | None:
        """The shape of the handler `component_id` would run for `event_handler`.

        The component is named by its session state, so answering costs no
        database query and builds nothing.  `None` when the component has left
        the session, is not registered, or declares no such handler.

        """
        if (state := self.session.get_state(component_id)) and (
            registered := REGISTRY.get(state["hx_name"])
        ):
            return registered.handler_kind_mapping.get(event_handler)
        else:
            return None

    def atomic_dispatch(
        self, component_id: str, event_handler: str
    ) -> AbstractContextManager[None]:
        """The transaction guard for dispatching `event_handler`.

        A dispatch is one unit of work: on a database with `ATOMIC_REQUESTS`
        the component build, every handler the cascade reaches through `Emit`,
        and the render all commit or roll back together.  Listeners cannot
        escape it whatever shape they are declared, because a synchronous
        pipeline runs them on its own thread, and Django routes the async ORM
        of an `async def` listener back to that same thread and connection.

        An `async_generator` handler is the exception: it streams for as long
        as its source produces -- the run of a language model, say -- and a
        transaction spanning that would pin a connection and hold its locks
        for the whole stream.  Such a dispatch runs unwrapped, and each of its
        handlers keeps a transaction of its own instead.

        """
        if self.get_handler_kind(component_id, event_handler) == "async_generator":
            return nullcontext()
        else:
            return atomic_if_requested()

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
            component_class = REGISTRY[component_name].htmx_component_class
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

    def get_sse_subscriptions(self, component: HtmxComponent) -> set[SSESubscription]:
        """Return `component`'s SSE subscriptions, computing them once per repository cycle.

        Framework code must read the subscriptions through this method.  `sse_subscriptions` is a
        property, and may answer differently on each read -- one that calls `now()`, or that
        consults a model field, is enough -- which would register the consumer for one set of
        topics while the component's root tag advertises another.

        The value is keyed by the component's identity, so a component rebuilt later in the same
        cycle computes its own.  The entry holds the component because CPython reuses the id of a
        collected object.

        """
        from .sse import get_sse_subscriptions

        if (entry := self._sse_subscriptions.get(id(component))) is None:
            entry = (component, get_sse_subscriptions(component))
            self._sse_subscriptions[id(component)] = entry
        return entry[1]

    @classmethod
    def get_model_instance[M: models.Model](
        cls, model: type[M], pk: object, model_config: ModelConfig
    ) -> M | None:
        """Return the `model` row with primary key `pk`, or None when the row does not exist.

        The queryset applies the `select_related` and `prefetch_related` of `model_config`.

        When `model_config` opts into the model cache -- `ModelConfig.cache`, or
        `DJHTMX_DEFAULT_MODEL_CACHE` when that is None -- and a repository is `current`:meth:, the
        row is fetched once per repository cycle: every call for it returns the same instance.  A
        missing row is not remembered, so a row created later in the cycle is found.  Otherwise
        every call fetches, as it always does when `DJHTMX_DISABLE_MODEL_CACHE` is True.

        A cached instance is enriched with the relations a later call asks for and it lacks, so every
        caller gets its `select_related` and `prefetch_related`.  A `model_config` whose
        `prefetch_related` holds a `Prefetch` object never uses the cache: one instance can hold a
        single result per relation, and the queryset of one `Prefetch` would leak into every other
        caller.

        `pk` may arrive as the wire's string, or a composite pk as a list; it is coerced to the
        primary key's type first.

        """
        pk = normalize_pk(model, pk)
        cache_enabled = (
            not DISABLE_MODEL_CACHE
            and (DEFAULT_MODEL_CACHE if model_config.cache is None else model_config.cache)
            and not any(
                isinstance(relation, Prefetch) for relation in model_config.prefetch_related or ()
            )
        )
        if cache_enabled and (repository := cls.current()) is not None:
            instances = repository._model_instances.setdefault(model, {})
            if (cached := instances.get(pk)) is None:
                _CachedInstance.count(model, "misses")
                if (instance := cls._fetch_model_instance(model, pk, model_config)) is not None:
                    instances[pk] = _CachedInstance.from_instance(instance, model_config)
            else:
                _CachedInstance.count(model, "hits")
                cached.enrich(model_config)
                instance = cast(M, cached.instance)
        else:
            instance = cls._fetch_model_instance(model, pk, model_config)
        return instance

    def invalidate_model_cache(self, model_class: type[models.Model], pk: object = None):
        """Drop rows of `model_class` from this repository's model cache.

        `pk` is None, a pk, or a list of them, as `InvalidateModelCache`:class: documents.

        """
        match pk:
            case None:
                self._model_instances.pop(model_class, None)
            case list() | set() | frozenset() as pks:
                self._drop_model_instances(model_class, pks)
            case tuple() as pks if not isinstance(model_class._meta.pk, models.CompositePrimaryKey):
                self._drop_model_instances(model_class, pks)
            case single_pk:
                self._drop_model_instances(model_class, (single_pk,))

    def _drop_model_instances(self, model: type[models.Model], pks: Iterable[object]) -> None:
        instances = self._model_instances.get(model, {})
        for pk in pks:
            # A pk that cannot be coerced keys no cached row, so there is nothing to drop.
            with suppress(ValueError):
                instances.pop(normalize_pk(model, pk), None)

    @staticmethod
    def _fetch_model_instance[M: models.Model](
        model: type[M], pk: object, model_config: ModelConfig
    ) -> M | None:
        """Fetch the `model` row with primary key `pk`, or None when there is none.

        The queryset applies the `select_related` and `prefetch_related` of `model_config`.

        """
        with tracing_span(
            "djhtmx.model.fetch",
            model=get_fqn(model),
            pk=_describe_pk(pk),
            lazy=str(model_config.lazy),
        ):
            manager = model.objects
            if select_related := model_config.select_related:
                manager = manager.select_related(*select_related)
            if prefetch_related := model_config.prefetch_related:
                manager = manager.prefetch_related(*prefetch_related)
            # Use filter().first() instead of get() to avoid exceptions
            return manager.filter(pk=pk).first()

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

        register_component(
            self.session.id, component, subscriptions=self.get_sse_subscriptions(component)
        )
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


def _describe_pk(value) -> str:
    """Render the primary key `value` stands for, as a span tag.

    Never `repr(value)`: `Model.__repr__` calls `__str__`, and a `__str__` that follows a relation
    issues the very query these spans are counting.  And `str` of the pk rather than `repr`, so one
    row tags the same whether it arrived as the wire's string or as the type the lazy proxy coerces
    it to -- otherwise the same row counts as two.

    """
    return str(getattr(value, "pk", value))


@dataclass(slots=True)
class _CachedInstance:
    """A model instance shared through the repository cache, and the relations loaded on it."""

    instance: models.Model
    relations: set[str]
    """The relation paths loaded on `instance`, with every path traversed to reach a deeper one."""

    @classmethod
    def from_instance(cls, instance: models.Model, model_config: ModelConfig) -> _CachedInstance:
        """Cache `instance`, fetched with the relations `model_config` asks for."""
        return cls(instance, cls.get_relations(model_config))

    def enrich(self, model_config: ModelConfig) -> None:
        """Load onto `instance` the relations `model_config` asks for and it lacks."""
        if missing := self.get_relations(model_config) - self.relations:
            with tracing_span(
                "djhtmx.model.prefetch",
                model=get_fqn(type(self.instance)),
                pk=_describe_pk(self.instance),
            ):
                # Django skips the paths already loaded, and a forward relation is loaded as
                # `select_related` would have.
                prefetch_related_objects(
                    [self.instance],
                    *(model_config.select_related or ()),
                    *(model_config.prefetch_related or ()),
                )
            self.relations |= missing
            self.count(type(self.instance), "enrichments")

    @staticmethod
    def count(model: type[models.Model], entry: Literal["hits", "misses", "enrichments"]) -> None:
        """Count a model cache `entry`, in the totals and in `model`'s own metric."""
        metric_incr(f"djhtmx.model.cache.{entry}")
        metric_incr(f"djhtmx.model.{get_model_full_label(model)}.cache.{entry}")

    @staticmethod
    def get_relations(model_config: ModelConfig) -> set[str]:
        """Return the relation paths `model_config` loads, with every path traversed on the way."""
        relations: set[str] = set()
        for relation in (
            *(model_config.select_related or ()),
            *(model_config.prefetch_related or ()),
        ):
            match relation:
                case Prefetch(prefetch_to=path) | (str() as path):
                    relations.update(
                        accumulate(
                            path.split(LOOKUP_SEP),
                            lambda prefix, part: f"{prefix}{LOOKUP_SEP}{part}",
                        )
                    )
                case unreachable:
                    assert_never(unreachable)
        return relations


_current: ContextVar[Repository | HttpRequest | None] = ContextVar(
    "djhtmx.current_repository", default=None
)
