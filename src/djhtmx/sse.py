from __future__ import annotations

import asyncio
import dataclasses
import inspect
import logging
import weakref
from collections import defaultdict
from collections.abc import Iterable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, NamedTuple, Union, get_args, get_origin, get_type_hints

import redis
import redis.asyncio as async_redis
from asgiref.sync import sync_to_async
from django.utils.html import format_html
from pydantic import BaseModel
from xotl.tools.objects import import_object

from . import json, settings
from .component import BuildAndRender, Destroy, Emit, HtmxComponent, Render, SkipRender
from .introspection import _extract_event_types, _resolve_typevars, _substitute_typevars
from .utils import compact_hash


class SSESubscription(NamedTuple):
    event_type: type
    topic: str


@dataclass(slots=True, frozen=True)
class SSEEventEnvelope[E]:
    event: E
    topic: str
    source_session_id: str | None = None


def event_type_name(event_type: type) -> str:
    return f"{event_type.__module__}.{event_type.__qualname__}"


def consumer_id(session_id: str, component_id: str) -> str:
    return compact_hash(f"{session_id}:{component_id}:sse")


def consumer_key(id_: str) -> str:
    return f"djhtmx:sse:consumer:{id_}"


def consumer_indexes_key(id_: str) -> str:
    return f"djhtmx:sse:consumer:{id_}:indexes"


def session_consumers_key(session_id: str) -> str:
    return f"djhtmx:sse:session:{compact_hash(session_id)}:consumers"


def session_events_key(session_id: str) -> str:
    return f"djhtmx:sse:session:{compact_hash(session_id)}:events"


def wake_channel(session_id: str) -> str:
    return f"djhtmx:sse:wake:session:{compact_hash(session_id)}"


def index_key(event_type: type | str, topic: str) -> str:
    event_type_id = event_type if isinstance(event_type, str) else event_type_name(event_type)
    return f"djhtmx:sse:index:{compact_hash(event_type_id)}:{compact_hash(topic)}:consumers"


def get_sse_event_handler_event_types(f, owner: type | None = None) -> set[type]:
    hints = get_type_hints(f)
    event = next(annotation for name, annotation in hints.items() if name != "return")
    if owner is not None:
        typevar_map = _resolve_typevars(owner)
        if typevar_map:
            event = _substitute_typevars(event, typevar_map)

    origin = get_origin(event)
    if origin is not SSEEventEnvelope:
        return set()

    args = get_args(event)
    if not args:
        return set()

    payload = args[0]
    payload_origin = get_origin(payload)
    if payload_origin is Union:
        return _extract_event_types(payload)
    else:
        return _extract_event_types(payload)


def register_sse_listener(component_type: type[HtmxComponent]):
    if handle_sse_events := getattr(component_type, "_handle_sse_events", None):
        for event_type in get_sse_event_handler_event_types(
            handle_sse_events, owner=component_type
        ):
            SSE_LISTENERS[event_type].add(component_type)


def is_sse_enabled(component: HtmxComponent) -> bool:
    has_subscriptions = hasattr(type(component), "sse_subscriptions")
    has_handler = hasattr(component, "_handle_sse_events")
    if has_subscriptions != has_handler:
        logger.warning(
            "Component %s must define both sse_subscriptions and _handle_sse_events to use SSE",
            component.hx_name,
        )
    return has_subscriptions and has_handler


def get_sse_subscriptions(component: HtmxComponent) -> set[SSESubscription]:
    if is_sse_enabled(component):
        accepted_event_types = get_sse_event_handler_event_types(
            component._handle_sse_events,  # type: ignore[attr-defined]
            owner=type(component),
        )
        subscriptions = component.sse_subscriptions  # type: ignore[attr-defined]
        result = set()
        for subscription in subscriptions:
            if subscription.event_type in accepted_event_types:
                result.add(subscription)
            else:
                logger.warning(
                    "Component %s subscribes to %s but _handle_sse_events does not accept it",
                    component.hx_name,
                    event_type_name(subscription.event_type),
                )
        return result
    else:
        return set()


def register_component(session_id: str, component: HtmxComponent, ttl: int = settings.SESSION_TTL):
    subscriptions = get_sse_subscriptions(component)
    id_ = consumer_id(session_id, component.id)
    indexes_key = consumer_indexes_key(id_)
    sync_redis_connection = get_sync_conn()
    old_indexes = sync_smembers_text(sync_redis_connection, indexes_key)
    new_indexes = {
        index_key(subscription.event_type, subscription.topic) for subscription in subscriptions
    }

    stale_indexes = old_indexes - new_indexes
    for key in stale_indexes:
        sync_redis_connection.srem(key, id_)

    if subscriptions:
        metadata = {
            "session_id": session_id,
            "component_id": component.id,
            "component_name": component.hx_name,
            "subscriptions": [
                {
                    "event_type": event_type_name(subscription.event_type),
                    "topic": subscription.topic,
                }
                for subscription in subscriptions
            ],
        }
        sync_redis_connection.set(consumer_key(id_), json.dumps(metadata), ex=ttl)
        sync_redis_connection.sadd(session_consumers_key(session_id), id_)
        sync_redis_connection.expire(session_consumers_key(session_id), ttl)
        sync_redis_connection.delete(indexes_key)
        if new_indexes:
            sync_redis_connection.sadd(indexes_key, *new_indexes)
            sync_redis_connection.expire(indexes_key, ttl)
        for key in new_indexes:
            sync_redis_connection.sadd(key, id_)
            sync_redis_connection.expire(key, ttl)
    else:
        sync_redis_connection.delete(consumer_key(id_))
        sync_redis_connection.srem(session_consumers_key(session_id), id_)
        sync_redis_connection.delete(indexes_key)


@dataclass(slots=True)
class EventEnvelope[P]:
    consumer_id: str
    event_type: str
    topic: str
    payload: P
    source_session_id: str | None = None


@contextmanager
def sse_source_session(session_id: str):
    token = _SOURCE_SESSION_ID.set(session_id)
    try:
        yield
    finally:
        _SOURCE_SESSION_ID.reset(token)


def current_source_session_id() -> str | None:
    return _SOURCE_SESSION_ID.get()


def emit_sse_event(event: Any, *, topics: Iterable[str], source_session_id: str | None = None):
    if type(event) not in SSE_LISTENERS:
        return

    source_session_id = source_session_id or current_source_session_id()
    sync_redis_connection = get_sync_conn()

    event_type = event_type_name(type(event))
    consumer_topics: set[tuple[str, str]] = set()
    for topic in topics:
        key = index_key(event_type, topic)
        consumer_topics.update(
            (consumer, topic) for consumer in sync_smembers_text(sync_redis_connection, key)
        )

    sessions: set[str] = set()
    for id_, topic in consumer_topics:
        raw_metadata = sync_get(sync_redis_connection, consumer_key(id_))
        if raw_metadata:
            metadata = json.loads(raw_metadata)
            session_id = metadata["session_id"]
            envelope = EventEnvelope(
                consumer_id=id_,
                event_type=event_type,
                topic=topic,
                payload=event,
                source_session_id=source_session_id,
            )
            sync_redis_connection.rpush(
                session_events_key(session_id),
                json.dumps(dataclasses.asdict(envelope)),
            )
            sync_redis_connection.expire(session_events_key(session_id), settings.SESSION_TTL)
            sessions.add(session_id)

    for session_id in sessions:
        sync_redis_connection.publish(wake_channel(session_id), "1")


_async_conns: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, async_redis.Redis] = (
    weakref.WeakKeyDictionary()
)


def get_sync_conn() -> redis.Redis:
    return settings.conn


def get_async_conn() -> async_redis.Redis:
    loop = asyncio.get_running_loop()
    if loop not in _async_conns:
        _async_conns[loop] = async_redis.from_url(settings.REDIS_URL)
    return _async_conns[loop]


def decode_event(envelope: EventEnvelope) -> SSEEventEnvelope[Any]:
    event_type = import_object(envelope.event_type)
    payload = envelope.payload
    if inspect.isclass(event_type) and issubclass(event_type, BaseModel):
        event = event_type.model_validate(payload)
    elif isinstance(payload, dict):
        event = event_type(**payload)
    else:
        event = event_type(payload)
    return SSEEventEnvelope(
        event=event,
        topic=envelope.topic,
        source_session_id=envelope.source_session_id,
    )


async def refresh_sse_session_liveness(conn: async_redis.Redis, session_id: str):
    if not settings.SESSION_REFRESH_INTERVAL:
        return

    ttl = settings.SESSION_TTL
    session_consumers = session_consumers_key(session_id)
    await async_expire(conn, f"{session_id}:states", ttl)
    await async_expire(conn, session_consumers, ttl)
    await async_expire(conn, session_events_key(session_id), ttl)

    for consumer in await async_smembers_text(conn, session_consumers):
        consumer_indexes = consumer_indexes_key(consumer)
        await async_expire(conn, consumer_key(consumer), ttl)
        await async_expire(conn, consumer_indexes, ttl)
        for index in await async_smembers_text(conn, consumer_indexes):
            await async_expire(conn, index, ttl)


async def load_consumer_metadata(id_: str) -> dict[str, Any] | None:
    conn = get_async_conn()
    raw_metadata = await async_get(conn, consumer_key(id_))
    if raw_metadata:
        return json.loads(raw_metadata)


def sse_message(event: str, data: str) -> bytes:
    lines = [f"event: {event}"]
    data_lines = data.splitlines() or [""]
    lines.extend(f"data: {line}" for line in data_lines)
    return ("\n".join(lines) + "\n\n").encode()


async def render_sse_events(session_id: str, user) -> str:
    return "\n".join(await render_sse_event_fragments(session_id, user))


async def render_sse_event_fragments(session_id: str, user) -> list[str]:
    conn = get_async_conn()
    raw_events = await async_lrange(conn, session_events_key(session_id), 0, -1)
    if raw_events:
        await conn.delete(session_events_key(session_id))

    envelopes_by_consumer: dict[str, list[EventEnvelope]] = defaultdict(list)
    for raw_event in raw_events:
        data = json.loads(raw_event)
        envelope = EventEnvelope(**data)
        envelopes_by_consumer[envelope.consumer_id].append(envelope)

    html: list[str] = []
    for id_, envelopes in envelopes_by_consumer.items():
        metadata = await load_consumer_metadata(id_)
        if metadata:
            rendered = await sync_to_async(_render_consumer_sse_events)(
                session_id,
                user,
                metadata,
                envelopes,
            )
            html.extend(rendered)

    return html


def _render_consumer_sse_events(
    session_id: str,
    user,
    metadata: dict[str, Any],
    envelopes: list[EventEnvelope],
) -> list[str]:
    from django.contrib.auth.models import AnonymousUser

    from .repo import Repository, Session
    from .utils import get_params

    repo = Repository(
        user=user or AnonymousUser(), session=Session(session_id), params=get_params(None)
    )
    component = repo.get_component_by_id(metadata["component_id"])
    if not isinstance(component, HtmxComponent) or not hasattr(component, "_handle_sse_events"):
        return []

    result: list[str] = []
    render_component = False
    rendered_self = False
    for envelope in envelopes:
        event = decode_event(envelope)
        emitted = component._handle_sse_events(event)  # type: ignore[attr-defined]
        commands = [] if emitted is None else list(emitted)
        for command in commands:
            match command:
                case None:
                    render_component = True
                case SkipRender():
                    render_component = False
                case Render(component=rendered):
                    rendered_self = rendered_self or rendered.id == component.id
                    result.append(str(repo.render_html(rendered, oob=command.oob or "true")))
                case BuildAndRender(
                    component=component_type, state=state, oob=oob, parent_id=parent_id
                ):
                    rendered = repo.build(component_type.__name__, state, parent_id=parent_id)
                    result.append(str(repo.render_html(rendered, oob=oob)))
                case Destroy(component_id):
                    repo.unregister_component(component_id)
                    result.append(
                        str(format_html('<div id="{}" hx-swap-oob="delete"></div>', component_id))
                    )
                case Emit():
                    logger.error("Emit is not supported from SSE handlers: %s", command)
                case _:
                    logger.error("Command is not supported from SSE handlers: %s", command)
    if render_component and not rendered_self:
        result.append(str(repo.render_html(component, oob="true")))
    repo.session.flush()
    return result


def _decode(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value


# async_redis and redis cheat in the type hints; these are just "collection" of the `type: ignore`
# we need because the upstream library is not correctly typed.


def sync_smembers_text(conn: redis.Redis, key: str) -> set[str]:
    return {_decode(member) for member in conn.smembers(key)}  # type: ignore


def sync_get(conn: redis.Redis, key: str) -> bytes | str | None:
    return conn.get(key)  # type: ignore


async def async_get(conn: async_redis.Redis, key: str) -> bytes | str | None:
    return await conn.get(key)  # type: ignore


async def async_smembers_text(conn: async_redis.Redis, key: str) -> set[str]:
    return {_decode(member) for member in await conn.smembers(key)}  # type: ignore


async def async_expire(conn: async_redis.Redis, key: str, ttl: int):
    await conn.expire(key, ttl)  # type: ignore


async def async_lrange(
    conn: async_redis.Redis,
    key: str,
    start: int,
    end: int,
) -> list[bytes | str]:
    return await conn.lrange(key, start, end)  # type: ignore


logger = logging.getLogger(__name__)

SSE_LISTENERS: dict[type, set[type[HtmxComponent]]] = defaultdict(set)
_SOURCE_SESSION_ID: ContextVar[str | None] = ContextVar(
    "djhtmx_sse_source_session_id", default=None
)
