from __future__ import annotations

import dataclasses
import inspect
import logging
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, NamedTuple

import redis
import redis.asyncio as async_redis
from asgiref.sync import sync_to_async
from django.utils.html import format_html
from pydantic import BaseModel
from xotl.tools.objects import import_object

from . import json, settings
from .component import Destroy, Emit, HtmxComponent, Render, SkipRender
from .utils import compact_hash

logger = logging.getLogger(__name__)


class SSESubscription(NamedTuple):
    event_type: type
    topic: str


@dataclass(slots=True, frozen=True)
class SSEEvent[E]:
    event: E
    topic: str


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
        subscriptions = component.sse_subscriptions  # type: ignore
        return set(subscriptions)
    else:
        return set()


def register_component(session_id: str, component: HtmxComponent, ttl: int = settings.SESSION_TTL):
    subscriptions = get_sse_subscriptions(component)
    id_ = consumer_id(session_id, component.id)
    indexes_key = consumer_indexes_key(id_)
    sync_redis_connection = get_sync_conn()
    old_indexes = {_decode(index) for index in sync_redis_connection.smembers(indexes_key)}
    new_indexes = {
        index_key(subscription.event_type, subscription.topic) for subscription in subscriptions
    }

    stale_indexes = old_indexes - new_indexes
    for key in stale_indexes:
        get_sync_conn().srem(key, id_)

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
        get_sync_conn().set(consumer_key(id_), json.dumps(metadata), ex=ttl)
        get_sync_conn().sadd(session_consumers_key(session_id), id_)
        get_sync_conn().expire(session_consumers_key(session_id), ttl)
        get_sync_conn().delete(indexes_key)
        if new_indexes:
            get_sync_conn().sadd(indexes_key, *new_indexes)
            get_sync_conn().expire(indexes_key, ttl)
        for key in new_indexes:
            get_sync_conn().sadd(key, id_)
            get_sync_conn().expire(key, ttl)
    else:
        get_sync_conn().delete(consumer_key(id_))
        get_sync_conn().srem(session_consumers_key(session_id), id_)
        get_sync_conn().delete(indexes_key)


@dataclass(slots=True)
class EventEnvelope[P]:
    consumer_id: str
    event_type: str
    topic: str
    payload: P


def emit_sse_event(event: Any, *, topics: Iterable[str]):
    event_type = event_type_name(type(event))
    consumer_topics: set[tuple[str, str]] = set()
    for topic in topics:
        key = index_key(event_type, topic)
        consumer_topics.update(
            (_decode(consumer), topic) for consumer in get_sync_conn().smembers(key)
        )

    sessions: set[str] = set()
    for id_, topic in consumer_topics:
        raw_metadata = get_sync_conn().get(consumer_key(id_))
        if raw_metadata:
            metadata = json.loads(raw_metadata)
            session_id = metadata["session_id"]
            envelope = EventEnvelope(
                consumer_id=id_,
                event_type=event_type,
                topic=topic,
                payload=event,
            )
            get_sync_conn().rpush(
                session_events_key(session_id), json.dumps(dataclasses.asdict(envelope))
            )
            get_sync_conn().expire(session_events_key(session_id), settings.SESSION_TTL)
            sessions.add(session_id)

    for session_id in sessions:
        get_sync_conn().publish(wake_channel(session_id), "1")


_async_conn: async_redis.Redis | None = None


def get_sync_conn() -> redis.Redis:
    return settings.conn


def get_async_conn() -> async_redis.Redis:
    global _async_conn
    if _async_conn is None:
        _async_conn = async_redis.from_url(settings.REDIS_URL)
    return _async_conn


def decode_event(envelope: EventEnvelope) -> SSEEvent[Any]:
    event_type = import_object(envelope.event_type)
    payload = envelope.payload
    if inspect.isclass(event_type) and issubclass(event_type, BaseModel):
        event = event_type.model_validate(payload)
    elif isinstance(payload, dict):
        event = event_type(**payload)
    else:
        event = event_type(payload)
    return SSEEvent(event=event, topic=envelope.topic)


async def load_consumer_metadata(id_: str) -> dict[str, Any] | None:
    conn = get_async_conn()
    raw_metadata = await conn.get(consumer_key(id_))
    if raw_metadata:
        return json.loads(raw_metadata)


def sse_message(event: str, data: str) -> bytes:
    lines = [f"event: {event}"]
    data_lines = data.splitlines() or [""]
    lines.extend(f"data: {line}" for line in data_lines)
    return ("\n".join(lines) + "\n\n").encode()


async def render_sse_events(session_id: str, user) -> str:
    conn = get_async_conn()
    raw_events = await conn.lrange(session_events_key(session_id), 0, -1)
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

    return "\n".join(html)


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
