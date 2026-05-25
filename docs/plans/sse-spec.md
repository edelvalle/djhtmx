# djhtmx SSE specification

djhtmx delivers server-pushed updates to a browser page over a single Server-Sent-Events connection.  Application code emits typed events to named topics; components declare subscriptions for those topics; the SSE loop runs their handlers and ships the resulting OOB HTML and browser-command fragments through the page's `EventSource`.

This document describes the public Python API and the server/browser architecture that backs it.

## Public API

This section describes the user-facing API.  It deliberately avoids Redis, worker, and event-loop details.

### `emit_sse_event`

`emit_sse_event` publishes a typed event to one or more SSE topics.

```python
from djhtmx.sse import emit_sse_event

emit_sse_event(
    ReportPDFEvent(task_id=task.id, status=task.status),
    topics={f"pdf-task:{task.id}"},
)
```

```python
def emit_sse_event(
    event: BaseModel,
    *,
    topics: Iterable[str],
    source_session_id: str | None = None,
) -> None:
    ...
```

The function is synchronous and fire-and-forget.  It does not wait for any browser to receive the event, does not render components, and does not report how many components were affected.  If no consumer type is registered for `type(event)`, the call returns immediately without touching Redis.

`source_session_id` is normally not passed by callers: when the call runs inside a djhtmx request, the emitting session's id is captured from a context variable and stored on every enqueued envelope.

`SSEHeartbeat` cannot be emitted through `emit_sse_event`; the SSE loop is the only source of those events.  Passing one raises `TypeError`.

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

Do not call Django's `transaction.on_commit` directly for SSE emits that need to preserve djhtmx context.  `run_on_commit` captures the current Python context before registering the commit callback, so context-local SSE metadata such as the source djhtmx session remains available when the callback eventually runs.

`topics` are application-defined strings.  A topic should be stable and specific enough to avoid waking unrelated components.

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

`event_type` is used to deserialize and type-check events delivered to the component.  `topic` is used to match emitted events to interested components.

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

- `sse_subscriptions` may be a plain property or a cached property.
- It returns `set[SSESubscription]`.
- Returning an empty set means the component currently has no active SSE subscriptions.
- A subscription whose `event_type` is not accepted by `_handle_sse_events` (as derived from its type annotation) is   filtered out with a warning.
- Defining only one of `sse_subscriptions` / `_handle_sse_events` logs a warning and disables SSE for the component.

### `SSEEventEnvelope`

The handler receives an `SSEEventEnvelope[E]`, not the raw event directly.

```python
@dataclass(slots=True, frozen=True)
class SSEEventEnvelope[E]:
    event: E
    topic: str
    source_session_id: str | None = None
```

`envelope.event` is the typed payload originally passed to `emit_sse_event`.  `envelope.topic` is the topic that matched this component's subscription.  `envelope.source_session_id` is the djhtmx session that emitted the event, when the event originated from a djhtmx request.

### `_handle_sse_events`

`_handle_sse_events` handles SSE events for a component.  It runs through the same `CommandProcessor` as HTTP event handlers, so its return/yield contract mirrors regular event handlers.

```python
from djhtmx.component import Render, SkipRender
from djhtmx.sse import SSEEventEnvelope

class PDFButton(HtmxComponent):
    def _handle_sse_events(self, envelope: SSEEventEnvelope[PDFTaskChanged]):
        match envelope.event.status:
            case "done" | "failed":
                pass  # default render
            case _:
                yield SkipRender(self)
```

Return/yield semantics:

- Returning `None` (or yielding nothing) enqueues an implicit `Render(self)` — the default render.
- `yield Render(self)` is the explicit form of the default render.
- `yield Render(other_component)` renders another component if the repository can resolve it.
- `yield SkipRender(self)` consumes the event without rendering this component (it suppresses the default render for this invocation).
- `yield Destroy(component_id)` removes a component from the page and from djhtmx state.

All command types supported by HTTP event handlers also work in SSE handlers, including the browser commands `Focus`, `ScrollIntoView`, `Open`, `DispatchDOMEvent`, and the URL commands `Redirect`, `PushURL`, `ReplaceURL`.  SSE payloads cannot use HTMX response headers, so these commands ride through the session-scoped browser command sink described below.

Exceptions raised inside `_handle_sse_events` are caught, logged, and re-emitted as `Emit(HtmxUnhandledError(...))`.  They do not interrupt the rest of the drain.

`yield Emit(event)` is allowed for completeness — it fans out to other in-session listeners exactly as it would inside an HTTP handler.  It does **not** become another SSE broadcast.  SSE events and in-process `Emit` are two different mechanisms: `Emit` is session-local; `emit_sse_event` is cross-session.  Cascading SSE emits from inside a handler is strongly discouraged.

SSE handlers should let the UI *react* to changes, not *issue* more changes in cascade.  Avoid:

- calling `emit_sse_event` from inside a handler;
- mutating the database from inside a handler.

### `SSEHeartbeat`, `get_sse_heartbeat_subscription`

`SSEHeartbeat` is a framework event for components that need periodic UI feedback while a page-level SSE connection is alive.

```python
class SSEHeartbeat(BaseModel):
    pace: int
```

Components subscribe to heartbeat events with:

```python
get_sse_heartbeat_subscription(self, 60)
```

The returned subscription targets a session-local heartbeat topic derived from the component's djhtmx session and the requested pace.  Heartbeat events therefore only reach components on a live page connection.  Components must already be bound to a djhtmx session when this call is made; `pace` must be a positive integer.

Heartbeat events are produced by the SSE loop itself, never by `emit_sse_event`.  The loop scans each session's heartbeat subscriptions, schedules a tick per requested pace, and synthesizes an `SSEEventEnvelope[SSEHeartbeat]` for each matching consumer when its pace is due.  The envelope's `source_session_id` is `None`.

### `SSEEventRouter`

`SSEEventRouter` is the in-page component that owns the SSE browser connection for the page.

Applications render it once per page, usually near the end of
`<body>`:

```django
{% load htmx %}

<body>
  ...
  {% htmx "SSEEventRouter" %}
</body>
```

The router is hidden and uses the HTMX SSE extension.  The page has one `EventSource`, not one per component: component updates arrive as OOB HTML fragments and are routed by HTMX using DOM ids.

The router is infrastructure.  Application components do not call it directly.

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

The endpoint responds with HTTP 501 when invoked under WSGI.  There is no long-polling fallback.

### Session liveness refresh

Long-lived SSE connections must refresh Redis TTLs for the active djhtmx session.  Otherwise, a page that remains open longer than `DJHTMX_SESSION_TTL` could lose its component state or SSE routing indexes while the browser connection is still alive.

`DJHTMX_SESSION_REFRESH_RATE` controls the refresh cadence as a fraction of `DJHTMX_SESSION_TTL`:

```python
DJHTMX_SESSION_REFRESH_RATE = 0.5
```

Semantics:

- `0`: disable liveness refresh.
- `0 < rate <= 1`: refresh every `DJHTMX_SESSION_TTL * rate` seconds.
- default: `0.5`, which refreshes every 30 minutes with the default one-hour TTL.

On each refresh the loop renews TTLs on the session state key, the session's SSE consumer/event keys, each consumer metadata key, each consumer reverse-index key, and each topic/type index key referenced by those consumers.

### Database connection lifetime

SSE breaks Django's two normal mechanisms for releasing database connections:

- `StreamingHttpResponse` keeps the request open for the lifetime of the browser tab, so the `request_finished` signal never fires and Django's per-request `close_old_connections()` hook never runs.
- Django's connections are thread-local, so any thread that runs render work for an SSE stream owns one `connections['default']` entry for as long as that thread is alive.

djhtmx dispatches SSE render work to a dedicated `ThreadPoolExecutor` (`djhtmx-sse-render`) instead of letting each SSE stream pin its own thread.  The pool size is bounded:

- `DJHTMX_SSE_RENDER_WORKERS` (default 8) — worker threads in the pool.  Each worker keeps at most one Django DB connection.  PG connection count is then bounded by this setting rather than growing with SSE stream count.
- `DJHTMX_SSE_RENDER_QUEUE_MAX` (default 0 = unbounded) — when the pending queue reaches this depth, new submissions raise `SSERenderQueueFull` so the SSE loop logs and drops rather than blowing up.

Per-worker connection hygiene:

- `DJHTMX_SSE_RENDER_HEALTHCHECK_EVERY` (default 50) — every Nth render the worker calls `is_usable()` on each connection and closes the broken ones.
- `DJHTMX_SSE_RENDER_ROTATE_EVERY` (default 200) — every Nth render the worker closes its connections so the PG pool can retire aged entries.  This interacts with `psycopg_pool`'s `max_lifetime`: a long-lived worker would otherwise hold a connection checked out indefinitely.
- Any `OperationalError` or `InterfaceError` raised during a render closes the worker's connections.

The submitted callable is opaque to the executor; it currently runs `_drain_sse_session`, which builds one `Repository` per session, processes the consumer's `HandleSSEEvents` commands through a single `CommandProcessor`, and serializes the resulting `ProcessedCommand` stream into OOB HTML fragments.

Under `TestCase`-style tests the executor is bypassed and the render runs back on the test thread via `sync_to_async(thread_sensitive=True)` so the uncommitted test transaction is visible through the shared DB connection.

### Runtime topology

The production topology is Granian/ASGI workers.

Per ASGI worker:

- many browser pages may hold open SSE HTTP connections;
- each open page connection is represented by an async response task;
- each connection task owns its own Redis pub/sub connection and is subscribed to exactly its session's wake channel;
- the SSE render pool described above is shared by all connection tasks on the worker.

Across workers:

- Redis is the only shared state for SSE: it stores routing indexes, consumer metadata, pending event queues, and wake pub/sub channels;
- a worker only wakes its own connection tasks, because only the worker hosting a given SSE connection is subscribed to that session's wake channel.

### Redis routing indexes

The Redis layer must find matching consumers through indexes.  It must not scan all consumers.

When an SSE-enabled component is rendered, djhtmx registers one consumer record for that rendered component instance.  The record stores:

- `session_id`;
- `component_id`;
- `component_name`;
- serialized subscription metadata (`event_type` FQN + `topic`).

Each consumer id is also added to its session's membership set, so the SSE task can discover which consumers belong to the session.

For each `SSESubscription(event_type, topic)`, djhtmx adds the consumer id to an exact-match topic/type index.  The concrete key hashes both components:

```text
djhtmx:sse:index:{hash(event_type_fqn)}:{hash(topic)}:consumers
```

The consumer also keeps a reverse-index set listing the index keys it belongs to.  On re-render, the reverse index is consulted to remove memberships from indexes that no longer apply before adding current subscriptions.

All consumer, session, and index metadata is TTL-bound to `DJHTMX_SESSION_TTL` and refreshed by the liveness loop.

### Matching consumers

When code calls `emit_sse_event(event, topics=...)`, matching consumers are found by exact Redis set lookups.

For each emitted topic:

1. compute the event type identity from `type(event)` (FQN);
2. build the Redis index key for `(event_type, topic)`;
3. read the set of consumer ids from that index;
4. union consumer ids across all emitted topics.

A component that wants several event types must declare a separate `SSESubscription` for each one; there is no subclass matching.

After matching consumer ids, djhtmx loads each consumer record to find its `session_id`.  Consumers whose record has expired are skipped.

### Event queues and session wake channels

Actual event payloads are stored separately from wake notifications.  Pub/Sub is only a wake mechanism, not the source of truth.

For each matched consumer, djhtmx pushes a JSON-encoded envelope onto the owning session's event list.  Each envelope carries:

- `consumer_id`;
- event type FQN;
- matching topic;
- payload data (Pydantic-dumped) and payload FQN;
- `source_session_id`.

The list is session-oriented, so the SSE task loads all pending work for the session in one `LRANGE`:

```text
djhtmx:sse:session:{hash(session_id)}:events
```

After enqueuing events, djhtmx publishes to the affected session's wake channel:

```text
djhtmx:sse:wake:session:{hash(session_id)}
```

Only the worker that currently hosts the SSE connection is subscribed to that session channel.

### Producer flow

When code calls `emit_sse_event(event, topics=...)`:

1. djhtmx looks up the event type in `SSE_LISTENERS` and returns immediately if no component subscribes to that type.
2. For each `(event_type, topic)` pair, it reads the consumer set from the Redis index and collects `(consumer_id, topic)` pairs.
3. For each matched consumer, it loads the consumer record to find the owning session and pushes a JSON-encoded `EventEnvelope` onto the session's events list (refreshing that list's TTL).
4. It publishes a wake notification to each affected session's wake channel.
5. The caller returns immediately.

The producer does not know which worker hosts a browser connection and does not render components.

### SSE task flow

Each connected SSE task runs roughly this loop:

```python
async for tick in stream:
    refresh_session_liveness_if_due()
    drain_due_heartbeats()  # builds SSEHeartbeat envelopes for due paces
    drain_pending_events()  # LRANGE + DEL the session events list
    sleep_until_woken_or_timeout(max=DJHTMX_SSE_HEARTBEAT_TIMEOUT, bounded_by_next_heartbeat_due)
```

Both drains funnel into the same render path:

1. Group envelopes by `consumer_id`.
2. For each consumer, load its metadata to map back to a `component_id`, then build a `HandleSSEEvents` command carrying that component's envelopes.
3. Submit the list of `HandleSSEEvents` commands to the SSE render pool.  Inside the worker:
   - Construct one `Repository` for the session.
   - Run all commands through one `CommandProcessor`.  The processor applies the standard handler contract (default `Render`, `SkipRender` suppression, `Render`/`Destroy` collapsing, `Emit` fan-out to in-session listeners).
   - Serialize the resulting `ProcessedCommand` stream via `to_sse_fragments`.
4. Emit one `event: djhtmx` SSE message per produced fragment.

Stale consumer records (component already destroyed in this dispatch) are detected inside `CommandProcessor` and silently skipped.

If the Redis pub/sub connection drops between drains, a wake can be lost.  The loop tolerates this by draining pending events at the top of every iteration; in the worst case the next heartbeat tick (≤ `DJHTMX_SSE_HEARTBEAT_TIMEOUT`, default 30 s) brings the loop back around and delivers the queued events.

`DJHTMX_SSE_HEARTBEAT_TIMEOUT` (default 30) is the cap on each `pubsub.get_message` wait.  When heartbeat subscriptions are active, the next due tick further shortens the wait; when none are scheduled, the value is the wait.  Lowering it tightens the recovery window for lost pub/sub wakes at the cost of more idle iterations; raising it does the opposite.  Must be `>= 1`; values `< 30` are accepted but discouraged.

### Command conversion

`to_sse_fragments` turns a `CommandBatch` into the list of fragments sent to the browser.  The mapping is:

- `Render(component)` → rendered component HTML with `hx-swap-oob="true"`.
- Partial `Render(..., template=...)` → OOB HTML targeting the partial's element id.
- `Destroy(component_id)` → `<div id="component_id" hx-swap-oob="delete"></div>`.
- Multiple renders of the same component within one drain collapse to the last render (the default `Render(self)` defers to an explicit one).
- Browser-effect commands (`Focus`, `ScrollIntoView`, `Open`, `DispatchDOMEvent`) and URL-mutating commands (`Redirect`, `PushURL`, `ReplaceURL`) are encoded as base64url-JSON payloads inside a `<template data-djhtmx-browser-command>` element OOB-swapped into the session-scoped command sink.

## Browser-side SSE routing

### HTMX SSE extension

The browser connection uses the HTMX SSE extension bundled with djhtmx at:

```text
src/djhtmx/static/htmx/<htmx-version>/ext/sse.js
```

The extension provides `hx-ext="sse"`, `sse-connect="..."`, `sse-swap="event-name"`, automatic reconnection, and normal HTMX swap processing for received event payloads.

### Router markup

`SSEEventRouter` produces the only `sse-connect` element on the page:

```html
<div id="djhtmx-sse-router"
     hidden
     hx-ext="sse"
     sse-connect="/_htmx/_sse/connect?session=...">
  <div sse-swap="djhtmx"></div>
  <div id="djhtmx-sse-commands-{session_hash}"
       data-djhtmx-sse-command-sink="{session_hash}"
       hidden></div>
</div>
```

The inner `sse-swap` element listens for `event: djhtmx`.  The container is hidden, so the swap target is not visible while HTMX still processes OOB fragments from the SSE payload.  The sibling `<div>` is the browser command sink described below.

### Browser command sink

The router includes a hidden, session-scoped command sink.  Browser and URL commands sent over SSE are serialized as base64url-encoded JSON inside `<template>` elements OOB-swapped into the sink:

```html
<div hx-swap-oob="beforeend: #djhtmx-sse-commands-{session_hash}">
  <template data-djhtmx-browser-command
            data-session="{session_hash}"
            data-payload="{base64url(json(command))}"></template>
</div>
```

A `MutationObserver` on the sink picks up each appended element, verifies its `data-session` matches the sink's session hash, decodes the payload, and dispatches to the unified `executeBrowserCommand` in `djhtmx`'s JS.  The decoded payload's `command` field is the discriminator; the executor validates per-command (e.g. same-origin URL for `open-tab`, known target names, valid selectors).  Each template element is removed after processing.

The same executor is shared with the HTTP path (via `HX-Trigger-After-Settle` events) and the WebSocket path (via JSON messages), so the supported command set is identical across transports.

### SSE payload format

The server sends one named SSE message per OOB fragment:

```text
event: djhtmx
data: <div id="hx-a" hx-swap-oob="true">...</div>

event: djhtmx
data: <div id="hx-b" hx-swap-oob="true">...</div>
```

Deletion:

```text
event: djhtmx
data: <div id="hx-a" hx-swap-oob="delete"></div>
```

The browser does not inspect `SSESubscription`.  Browser-side routing is DOM/OOB-based:

1. The SSE extension receives the `djhtmx` event.
2. HTMX swaps the payload into the hidden listener.
3. HTMX applies all OOB fragments.
4. Each OOB fragment targets the existing element with the same id.

### Component consumer metadata

SSE-enabled components are rendered with metadata on their root element:

```html
<div id="hx-..." data-djhtmx-sse-consumer="..."></div>
```

This attribute is for debugging and tooling.  The browser does not need it to route messages — routing is purely by DOM id and OOB behavior.

### Connection lifetime

The router opens the connection when the page is processed by HTMX.  The connection closes when the router element is removed, when the browser leaves the page, or when the server closes the stream.  The router stays alive for the lifetime of the page; it does not open and close dynamically based on whether SSE-enabled components are currently present.
