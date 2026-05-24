from __future__ import annotations

import asyncio
import logging
import weakref
from collections import defaultdict
from collections.abc import Iterable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import (
    Annotated,
    Any,
    NamedTuple,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)

import redis
import redis.asyncio as async_redis
from pydantic import BaseModel, Field
from xotl.tools.objects import import_object

from . import json, settings
from .component import HtmxComponent
from .introspection import _extract_event_types, _resolve_typevars, _substitute_typevars
from .sse_executor import submit_sse_render
from .utils import compact_hash, get_fqn


class SSESubscription(NamedTuple):
    event_type: type
    topic: str


@dataclass(slots=True, frozen=True)
class SSEEventEnvelope[E]:
    event: E
    topic: str
    source_session_id: str | None = None


class SSEHeartbeat(BaseModel):
    pace: int


def get_sse_heartbeat_subscription(component: HtmxComponent, pace: int) -> SSESubscription:
    return SSESubscription(SSEHeartbeat, get_sse_heartbeat_topic(component, pace))


def get_sse_heartbeat_topic(component: HtmxComponent, pace: int) -> str:
    if pace <= 0:
        raise ValueError("SSE heartbeat pace must be greater than zero")
    if component.session_id is None:
        raise ValueError("SSE heartbeat topics require a component bound to a djhtmx session")
    return _sse_heartbeat_topic(component.session_id, pace)


def event_type_name(event_type: type) -> str:
    return get_fqn(event_type)


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


def unregister_consumer(session_id: str, component_id: str) -> None:
    """Remove the SSE consumer record, its topic/type index memberships, and its
    session-membership entry for a component that no longer exists.

    Inverse of `register_component`.  Callers are responsible for invoking this
    on component destruction; otherwise the consumer entry lingers until TTL
    expiry and `emit_sse_event` keeps enqueuing events for it.
    """
    id_ = consumer_id(session_id, component_id)
    indexes_key = consumer_indexes_key(id_)
    sync_redis_connection = get_sync_conn()
    for key in sync_smembers_text(sync_redis_connection, indexes_key):
        sync_redis_connection.srem(key, id_)
    sync_redis_connection.delete(consumer_key(id_))
    sync_redis_connection.delete(indexes_key)
    sync_redis_connection.srem(session_consumers_key(session_id), id_)


class EventEnvelope[P: BaseModel](BaseModel):
    consumer_id: str
    event_type: str
    topic: str
    payload_data: Any = None
    payload_fqn: str = ""
    source_session_id: str | None = None

    payload: Annotated[P | None, Field(default=None, exclude=True)]

    def envelope_dump_json(self):
        assert self.payload is not None
        self.payload_data = self.payload.model_dump(mode="json")
        self.payload_fqn = event_type_name(type(self.payload))
        return self.model_dump_json()

    @classmethod
    def envelope_validate_json(cls, data):
        extracted = cls.model_validate_json(data)
        payload_type: BaseModel = import_object(extracted.payload_fqn)
        extracted.payload = payload_type.model_validate(extracted.payload_data)  # type: ignore
        return extracted


@contextmanager
def sse_source_session(session_id: str):
    token = _SOURCE_SESSION_ID.set(session_id)
    try:
        yield
    finally:
        _SOURCE_SESSION_ID.reset(token)


def current_source_session_id() -> str | None:
    return _SOURCE_SESSION_ID.get()


def emit_sse_event(
    event: BaseModel,
    *,
    topics: Iterable[str],
    source_session_id: str | None = None,
):
    if isinstance(event, SSEHeartbeat):
        raise TypeError("SSEHeartbeat is generated by the SSE loop and cannot be emitted")
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
                envelope.envelope_dump_json(),
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
    assert envelope.payload is not None
    return SSEEventEnvelope(
        event=envelope.payload,
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


async def load_consumer_metadata(
    id_: str,
    conn: async_redis.Redis | None = None,
) -> dict[str, Any] | None:
    conn = conn or get_async_conn()
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


async def render_sse_event_fragments(
    session_id: str,
    user,
    conn: async_redis.Redis | None = None,
) -> list[str]:
    from .commands import HandleSSEEvents

    conn = conn or get_async_conn()
    # Read-and-clear the events list atomically: a plain LRANGE followed by
    # DELETE would lose any envelope RPUSHed by emit_sse_event between the
    # two calls.  MULTI/EXEC serialises both commands as a single unit.
    events_key = session_events_key(session_id)
    async with conn.pipeline(transaction=True) as pipe:
        pipe.lrange(events_key, 0, -1)
        pipe.delete(events_key)
        raw_events, _ = await pipe.execute()

    envelopes_by_consumer: dict[str, list[EventEnvelope]] = defaultdict(list)
    for raw_event in raw_events:
        envelope = EventEnvelope.envelope_validate_json(raw_event)
        envelopes_by_consumer[envelope.consumer_id].append(envelope)

    handle_commands: list[HandleSSEEvents] = []
    for consumer_id, envelopes in envelopes_by_consumer.items():
        metadata = await load_consumer_metadata(consumer_id, conn)
        if metadata:
            handle_commands.append(
                HandleSSEEvents(
                    component_id=metadata["component_id"],
                    envelopes=tuple(decode_event(env) for env in envelopes),
                )
            )

    if not handle_commands:
        return []

    return await submit_sse_render(_drain_sse_session, session_id, user, handle_commands)


async def get_sse_heartbeat_paces(conn: async_redis.Redis, session_id: str) -> set[int]:
    heartbeat = event_type_name(SSEHeartbeat)
    paces = {
        pace
        for consumer in await async_smembers_text(conn, session_consumers_key(session_id))
        if (metadata := await load_consumer_metadata(consumer, conn))
        for subscription in metadata.get("subscriptions", [])
        if subscription.get("event_type") == heartbeat
        if (pace := _parse_sse_heartbeat_topic(session_id, subscription.get("topic", "")))
    }
    return paces


async def render_sse_heartbeat_fragments(
    conn: async_redis.Redis,
    session_id: str,
    user,
    paces: Iterable[int],
) -> list[str]:
    from .commands import HandleSSEEvents

    paces_by_topic = {_sse_heartbeat_topic(session_id, pace): pace for pace in paces}
    heartbeat = event_type_name(SSEHeartbeat)
    handle_commands: list[HandleSSEEvents] = []
    for consumer_id in await async_smembers_text(conn, session_consumers_key(session_id)):
        metadata = await load_consumer_metadata(consumer_id, conn)
        if not metadata:
            continue
        envelopes = tuple(
            SSEEventEnvelope(
                event=SSEHeartbeat(pace=paces_by_topic[topic]),
                topic=topic,
                source_session_id=None,
            )
            for subscription in metadata.get("subscriptions", [])
            if subscription.get("event_type") == heartbeat
            if (topic := subscription.get("topic")) in paces_by_topic
        )
        if envelopes:
            handle_commands.append(
                HandleSSEEvents(component_id=metadata["component_id"], envelopes=envelopes)
            )

    if not handle_commands:
        return []

    return await submit_sse_render(_drain_sse_session, session_id, user, handle_commands)


def _drain_sse_session(session_id: str, user, handle_commands: list) -> list[str]:
    """Sync render path executed on the SSE render worker thread.

    Builds one `Repository` for the session, runs all the consumers'
    `HandleSSEEvents` commands through a single `CommandProcessor`, and
    returns the resulting `ProcessedCommand` stream serialized as a list of
    SSE OOB HTML fragments.
    """
    from django.contrib.auth.models import AnonymousUser

    from .command_processor import CommandProcessor
    from .command_response import CommandBatch, to_sse_fragments
    from .repo import Repository, Session
    from .utils import get_params

    repo = Repository(
        user=user or AnonymousUser(), session=Session(session_id), params=get_params(None)
    )
    processor = CommandProcessor(repo)
    batch = CommandBatch.from_processed(processor.process(handle_commands))
    return to_sse_fragments(batch, session_id)


def _sse_heartbeat_topic(session_id: str, pace: int) -> str:
    return f"djhtmx.sse.heartbeat.{compact_hash(session_id)}.{pace}"


def _parse_sse_heartbeat_topic(session_id: str, topic: str) -> int | None:
    prefix = f"djhtmx.sse.heartbeat.{compact_hash(session_id)}."
    if topic.startswith(prefix):
        try:
            pace = int(topic.removeprefix(prefix))
        except ValueError:
            logger.warning("Invalid SSE heartbeat topic: %s", topic)
        else:
            return pace if pace > 0 else None
    return None


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


logger = logging.getLogger(__name__)

SSE_LISTENERS: dict[type, set[type[HtmxComponent]]] = defaultdict(set)
_SOURCE_SESSION_ID: ContextVar[str | None] = ContextVar(
    "djhtmx_sse_source_session_id", default=None
)
