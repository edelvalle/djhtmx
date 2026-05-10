# djhtmx SSE design specification

## Status

Design specification. No implementation yet.

## Public API

This section describes the user-facing API. It intentionally avoids Redis, worker, and event-loop details.

### `emit_sse_event`

`emit_sse_event` publishes a typed event to one or more SSE topics.

```python
from djhtmx.sse import emit_sse_event

emit_sse_event(
    ReportPDFEvent(task_id=task.id, status=task.status),
    topics={f"pdf-task:{task.id}"},
)
```

The function is synchronous and fire-and-forget. It does not wait for any browser to receive the event, does not render components, and does not report how many components were affected.

Use `djhtmx.utils.run_on_commit` when the event describes database state that must be committed before consumers render:

```python
from djhtmx.sse import emit_sse_event
from djhtmx.utils import run_on_commit

run_on_commit(
    emit_sse_event,
    ReportPDFEvent(task_id=task.id, status=task.status),
    topics={f"pdf-task:{task.id}"},
)
```

Do not call Django's `transaction.on_commit` directly for SSE emits that need to preserve djhtmx context. `run_on_commit` captures the current Python context before registering the commit callback, so context-local SSE metadata such as the source djhtmx session remains available when the callback eventually runs.

`topics` are application-defined strings. A topic should be stable and specific enough to avoid waking unrelated components.

Examples:

```python
"pdf-task:123"
"user:42:notifications"
"todo.item.10.updated"
"todo.item.deleted"
```

### `SSESubscription`

`SSESubscription` declares that a component wants to receive events of a given type from a topic.

```python
from djhtmx.sse import SSESubscription

SSESubscription(ReportPDFEvent, topic=f"pdf-task:{self.task_id}")
```

It is a `NamedTuple`:

```python
class SSESubscription(NamedTuple):
    event_type: type
    topic: str
```

The `event_type` is used to deserialize and type-check events delivered to the component. The `topic` is used to match emitted events to interested components.

### `sse_subscriptions`

A component opts in to SSE by defining both `sse_subscriptions` and `_handle_sse_events`.

```python
class NotificationsToastList(HtmxComponent):
    @property
    def sse_subscriptions(self) -> set[SSESubscription]:
        return {
            SSESubscription(
                NotificationToastChanged,
                topic=f"user:{self.user.id}:notifications",
            )
        }
```

Rules:

- `sse_subscriptions` may be a property or cached property.
- It returns a `set[SSESubscription]`.
- Returning an empty set means the component has no active SSE subscriptions for its current state.
- If a component defines `sse_subscriptions` but not `_handle_sse_events`, djhtmx may warn and the component is not SSE-enabled.
- If a component defines `_handle_sse_events` but not `sse_subscriptions`, djhtmx may warn and the component is not SSE-enabled.

### `SSEEventEnvelope`

The handler receives an `SSEEventEnvelope[E]`, not the raw event directly.

```python
@dataclass(slots=True, frozen=True)
class SSEEventEnvelope[E]:
    event: E
    topic: str
    source_session_id: str | None = None
```

`envelope.event` is the typed payload originally passed to `emit_sse_event`. `envelope.topic` is the topic that matched this component's subscription. `envelope.source_session_id` is the djhtmx session that emitted the event, when the event originated from a djhtmx request.

### `_handle_sse_events`

`_handle_sse_events` handles SSE events for a component.

```python
from djhtmx.component import Render, SkipRender
from djhtmx.sse import SSEEventEnvelope

class PDFButton(HtmxComponent):
    def _handle_sse_events(self, envelope: SSEEventEnvelope[PDFTaskChanged]):
        match envelope.event.status:
            case "done" | "failed":
                yield None
            case _:
                yield SkipRender(self)
```

Return/yield semantics:

- `yield None` means "render this component normally", it is the same as `yield Render(self)`
- `yield Render(self)` explicitly renders this component.
- `yield Render(other_component)` may render another component if available.
- `yield SkipRender(self)` consumes the event without rendering this component.
- `yield Destroy(component_id)` removes a component from the page and from djhtmx state.
- if the handler doesn't yield any command, it's like `yield None`.

The first version is focused on commands that can be represented as HTML/OOB updates:

- `Render`;
- default render via `None`;
- `Destroy`.

Browser commands such as `Focus`, `ScrollIntoView`, `Open`, `Redirect`, `PushURL`, and `ReplaceURL` need a later command-carrier design for SSE because SSE payloads cannot use HTMX response headers.

The command `Emit` is ignored, logged as an error, and won't be supported in any future release.  Simply stated, SSE events and internal in-process events (`Emit`) are very different architecturally.

Important: Avoid any side-effect inside the handler or SSE events.  The following a list of bad patterns inside SSE handlers:

- Calling `emit_sse_events`
- Performing DB updates.  Even if the updates don't trigger SSE events (which they could); this is considered harmful.

SSE handlers should allow the UI to *react* to changes, without *issuing* more changes in cascade.

### `SSEEventRouter`

`SSEEventRouter` is the in-page component that owns the single SSE browser connection.

Applications should render it once in the base template, usually near the end of `<body>`:

```django
{% load htmx %}

<body>
  ...
  {% htmx "SSEEventRouter" %}
</body>
```

The component is hidden and uses the HTMX SSE extension. Conceptually its HTML is:

```html
<div hidden hx-ext="sse" sse-connect="/_htmx/_sse/connect?session=...">
  <div sse-swap="djhtmx"></div>
</div>
```

There is one `EventSource` per browser page, not one per component. Component updates are sent as OOB HTML fragments and routed by HTMX using DOM IDs.

The router is infrastructure. Application components do not call it directly.

## Server-side SSE loop

### Starting the HTTP/SSE handler

The Django app exposes one ASGI-only SSE endpoint:

```text
/_htmx/_sse/connect?session=<signed-session>
```

Applications start the handler by including the normal djhtmx URLs and rendering `SSEEventRouter` once per page.

```python
from django.urls import include, path

urlpatterns = [
    path("_htmx/", include("djhtmx.urls")),
]
```

```django
{% htmx "SSEEventRouter" %}
```

The endpoint must fail clearly under WSGI. There is no long-polling fallback.

### Session liveness refresh

Long-lived SSE connections must refresh Redis TTLs for the active djhtmx session. Otherwise, a page that remains open longer than `DJHTMX_SESSION_TTL` could lose its component state or SSE routing indexes while the browser connection is still alive.

`DJHTMX_SESSION_REFRESH_RATE` controls the refresh cadence as a fraction of `DJHTMX_SESSION_TTL`:

```python
DJHTMX_SESSION_REFRESH_RATE = 0.5
```

Semantics:

- `0`: disable liveness refresh.
- `0 < rate <= 1`: refresh every `DJHTMX_SESSION_TTL * rate` seconds.
- default: `0.5`, which refreshes every 30 minutes with the default one-hour TTL.

The SSE loop refreshes the session state key, the session's SSE consumer/event keys, each consumer metadata key, each consumer reverse-index key, and each topic/type index key referenced by those consumers.

### Runtime topology

The intended production topology is Granian/ASGI workers.

Per Granian worker:

- many browser pages may hold open SSE HTTP connections;
- each open page connection is represented by an async response task;
- a single in-process broker task owns one async Redis wake subscription connection;
- connection tasks are registered in memory by `session_id`;
- Redis stores durable routing state and pending event payloads.

Across workers:

- each worker has its own broker and Redis wake connection;
- each worker only wakes connection tasks connected to that worker;
- wake notifications are sent through session-specific Redis pub/sub channels;
- a worker subscribes to a session wake channel when it owns that session's SSE connection;
- a worker unsubscribes from the session wake channel when that SSE connection closes.

### Redis routing indexes

The Redis layer must find matching consumers through indexes. It must not scan all consumers.

When an SSE-enabled component is rendered, djhtmx registers one consumer record for that rendered component instance. The consumer record stores at least:

- `session_id`;
- `component_id`;
- `component_name`;
- serialized subscription metadata.

Each consumer is also added to its session membership set. This lets the SSE task discover which consumers belong to the session.

For each `SSESubscription(event_type, topic)`, djhtmx adds the consumer ID to an exact-match topic/type index:

```text
djhtmx:sse:index:{event_type}:{topic}:consumers
```

The concrete key format may hash or escape `event_type` and `topic`, but the semantics are exact match on both event type and topic.

The consumer also keeps a reverse-index set containing the index keys it belongs to. On re-render, djhtmx uses the reverse index to remove stale subscription memberships before adding the current subscriptions.

All consumer, session, and index metadata is TTL-bound. Stale consumers may be removed lazily when discovered during event emission or session processing.

### Matching consumers

When code calls `emit_sse_event(event, topics=...)`, matching consumers are found by exact Redis set lookups.

For each emitted topic:

1. compute the event type identity from `type(event)`;
2. build the Redis index key for `(event_type, topic)`;
3. read the set of consumer IDs from that index;
4. union consumer IDs across all emitted topics.

No subclass matching is required for the first version. A component that wants several event types must declare several `SSESubscription` values.

After matching consumer IDs, djhtmx loads each consumer record to find its `session_id`. Missing consumer records are stale and should be ignored and cleaned from indexes opportunistically.

### Event queues and session wake channels

Actual event payloads are stored separately from wake notifications. Pub/sub is only a wake mechanism, not the source of truth.

For each matched consumer, djhtmx enqueues an event entry that includes at least:

- `consumer_id`;
- event type;
- matching topic;
- serialized event payload.

The preferred queue shape is session-oriented, so the SSE task can load all pending work for the session in one operation:

```text
djhtmx:sse:session:{session_id}:events
```

After enqueuing events, djhtmx publishes to the affected session's wake channel:

```text
djhtmx:sse:wake:session:{session_id}
```

Only the worker that currently owns the SSE connection should be subscribed to that session channel.

### Producer flow

When code calls `emit_sse_event(event, topics=...)`:

1. djhtmx serializes the event.
2. djhtmx finds active consumers through exact topic/type Redis indexes.
3. djhtmx loads each matched consumer record to find its `session_id`.
4. djhtmx enqueues the event for each matching consumer in the owning session's event queue.
5. djhtmx publishes a wake notification to each affected session wake channel.
6. The caller returns immediately.

The producer does not know which worker owns a browser connection and does not render components.

### Worker broker flow

Each worker broker conceptually runs:

```python
while True:
    wake_event = await get_next_redis_sse_event()
    session_id = wake_event.session_id
    if sse_task := local_sessions.get(session_id):
        sse_task.wake()
```

The Redis wake connection is separate from normal Redis commands. It exists to avoid one blocking Redis wait per browser connection. The broker dynamically subscribes and unsubscribes this one Redis pub/sub connection to session-specific wake channels as SSE connections open and close.

### SSE task flow

Each connected SSE task conceptually runs:

```python
while connected:
    await wait_until_woken_or_heartbeat()

    events_by_consumer = await load_pending_events_for_session(session_id)
    commands = []

    for consumer_id, events in events_by_consumer.items():
        component = load_component_for_consumer(consumer_id)
        component_commands = []
        needs_default_render = False

        for raw_event in events:
            sse_event = build_sse_event(raw_event)
            emitted = component._handle_sse_events(sse_event)

            if emitted is None:
                needs_default_render = True
            else:
                for command in emitted:
                    if command is None:
                        needs_default_render = True
                    else:
                        component_commands.append(command)

        component_commands = coalesce_for_component(component, component_commands)

        if needs_default_render and not already_rendering_self(component, component_commands):
            component_commands.append(Render(component, oob="true"))

        commands.extend(component_commands)

    commands = coalesce_for_connection(commands)
    html = render_commands_as_oob_html(commands)
    await send_sse_message(event="djhtmx", data=html)
    await acknowledge_events(events_by_consumer)
```

### Command conversion

Before sending to the browser:

- `Render(component)` becomes rendered component HTML with `hx-swap-oob="true"`.
- Partial `Render(..., template=...)` becomes OOB HTML for its target ID.
- `Destroy(component_id)` becomes `<div id="component_id" hx-swap-oob="delete"></div>`.
- Multiple renders of the same component in one batch collapse to the last render.

Unsupported commands in the MVP should be logged and ignored or converted to a clearly documented error behavior. Later versions may add a command-carrier mechanism.

### Database-level events

The framework may provide `SSEModelEvent` for database changes:

```python
@dataclass(slots=True, frozen=True)
class SSEModelEvent:
    app_label: str
    model_name: str
    pk: object
    action: Literal["created", "updated", "deleted"]
```

Model topics should mirror existing djhtmx model subscription names where possible:

```text
<app_label>.<model_name>
<app_label>.<model_name>.<pk>
<app_label>.<model_name>.<pk>.updated
<app_label>.<model_name>.<pk>.deleted
<app_label>.<model_name>.created
<app_label>.<model_name>.updated
<app_label>.<model_name>.deleted
```

`post_save` and `post_delete` receivers can publish these events after commit. Receivers should check Redis topic indexes and avoid doing expensive work if no active SSE consumer is subscribed.

## Browser-side SSE routing

### HTMX SSE extension

The browser connection uses the HTMX SSE extension downloaded to:

```text
src/djhtmx/static/htmx/2.0.4/ext/sse.js
```

The extension provides:

- `hx-ext="sse"`;
- `sse-connect="..."`;
- `sse-swap="event-name"`;
- automatic reconnection;
- normal HTMX swap processing for received event payloads.

### Router markup

`SSEEventRouter` produces the only `sse-connect` element on the page:

```html
<div id="djhtmx-sse-router"
     hidden
     hx-ext="sse"
     sse-connect="/_htmx/_sse/connect?session=...">
  <div sse-swap="djhtmx"></div>
</div>
```

The inner element listens for `event: djhtmx`. The router is hidden, so the normal swap target is not visible, while HTMX still processes OOB fragments from the SSE payload.

### SSE payload format

The server sends named SSE messages:

```text
event: djhtmx
data: <div id="hx-a" hx-swap-oob="true">...</div><div id="hx-b" hx-swap-oob="true">...</div>

```

For deletion:

```text
event: djhtmx
data: <div id="hx-a" hx-swap-oob="delete"></div>

```

The browser does not inspect `SSESubscription`. Browser-side routing is DOM/OOB-based:

1. The SSE extension receives the `djhtmx` event.
2. HTMX swaps the payload into the hidden listener.
3. HTMX applies all OOB fragments.
4. Each OOB fragment targets the existing element with the same `id`.

### Component consumer metadata

SSE-enabled components may include metadata on their root element:

```html
<div id="hx-..." data-djhtmx-sse-consumer="..."></div>
```

This metadata is useful for debugging and for future browser-side cleanup. The first design does not require the browser to route messages using this value.

### Connection lifetime

The router opens the connection when the page is processed by HTMX. The connection closes when the router element is removed, when the browser leaves the page, or when the server closes the stream.

The first version should keep the router alive for the lifetime of the page. It does not need to open and close dynamically based on whether SSE-enabled components are currently present.
