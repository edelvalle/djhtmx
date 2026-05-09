# SSE updates for djhtmx components

## Status

Architecture proposal. No implementation yet.

## Goals

- Preserve complete backwards compatibility.
- Replace polling use cases with server-sent events.
- Make SSE strictly opt-in per component.
- Make SSE ASGI-only. Under WSGI, fail clearly rather than falling back to long polling.
- Allow normal application code, including non-HTMX code and background tasks, to wake matching HTMX/SSE consumers.
- Let the framework handle the async SSE loop, throttling, reconnect behavior, and render coalescing.
- Prefer using the HTMX SSE extension plus OOB swaps over a custom browser-side `EventSource` manager.

Primary motivating use cases:

- PDF generation buttons that currently poll for task status.
- Notification toast lists that currently poll for new notifications.

## Non-goals

- No changes to existing HTTP event handlers.
- No fallback to long polling.
- No requirement for existing components to change.
- No component-provided infinite loop in the first design.
- No one-connection-per-component design. A page may contain many SSE-enabled components, so the browser must use one shared SSE connection.
- No custom browser-side SSE manager unless the HTMX SSE extension proves insufficient.

## Component API

Components opt in by declaring SSE subscriptions and implementing a handler for matching SSE events. Both pieces are required.

`SSESubscription` should be a `NamedTuple`, and delivered events should be wrapped in `SSEEvent` so handlers can inspect both the typed event payload and the topic that delivered it:

```python
from dataclasses import dataclass
from typing import NamedTuple

class SSESubscription(NamedTuple):
    event_type: type
    topic: str

@dataclass(slots=True, frozen=True)
class SSEEvent[E]:
    event: E
    topic: str
```

Example component:

```python
class MyComponent(HtmxComponent):
    @property
    def sse_subscriptions(self) -> set[SSESubscription]:
        return {
            SSESubscription(ReportPDFEvent, topic=f"pdf-task:{self.task_id}"),
        }

    def _handle_sse_event(self, event: SSEEvent[ReportPDFEvent | SomeOtherEvent]):
        match event.event:
            case ReportPDFEvent(status="done"):
                yield None  # default full render
            case ReportPDFEvent(status="failed"):
                yield None  # default full render
            case _:
                yield SkipRender(self)
```

Rules:

- A component is SSE-enabled only when it provides both `sse_subscriptions` and `_handle_sse_event`.
- Components missing either piece keep producing the exact same HTML and behavior as today.
- The framework may emit an optional warning when only one of `sse_subscriptions` or `_handle_sse_event` is present.
- `_handle_sse_event` is only called for events delivered by the SSE bus.
- Existing `_handle_event` remains the regular backend event listener mechanism and is unchanged.
- Existing `subscriptions` remains unchanged and should not be overloaded for SSE.
- `_handle_sse_event` receives `SSEEvent[Event1 | Event2]`, where `.event` is the typed payload and `.topic` is the matching topic.
- `yield None` means “perform the default full render of this component”.
- `yield SkipRender(self)` means “consume this SSE event but do not render this component”.
- Explicit `Render(...)`, `Destroy(...)`, `Focus(...)`, `Open(...)`, etc. commands are allowed.

## Publishing events from normal code

Non-HTMX code should publish typed events to topics. It should not need to know which components exist. The issuer-facing API should be synchronous and fire-and-forget:

```python
from djhtmx.sse import emit_sse_event

emit_sse_event(
    ReportPDFEvent(task_id=task.id, status=task.status),
    topics={f"pdf-task:{task.id}"},
)
```

`emit_sse_event(...)` writes the necessary records to Redis and publishes a wake notification. The affected SSE connection may be in another process or worker; the caller does not await browser delivery or component rendering.

SSE should also support framework-published database-level events. This is important for components that represent model instances or model collections. For example, `TodoItem` in `src/tests/fision/` should be able to monitor DB-level changes to its `Item`, and `TodoList` should be able to react to `post_delete` events to remove or refresh deleted rows on screen.

A built-in event shape could be:

```python
@dataclass(slots=True, frozen=True)
class SSEModelEvent:
    app_label: str
    model_name: str
    pk: object
    action: Literal["created", "updated", "deleted"]
```

The framework can publish these from Django `post_save` and `post_delete` signals, using `transaction.on_commit` so browser updates reflect committed database state. Delete events must capture the model label and primary key before the instance is gone.

Model event topics should mirror the existing djhtmx model subscription naming where possible:

```text
<app_label>.<model_name>
<app_label>.<model_name>.<pk>
<app_label>.<model_name>.<pk>.updated
<app_label>.<model_name>.<pk>.deleted
<app_label>.<model_name>.created
<app_label>.<model_name>.updated
<app_label>.<model_name>.deleted
```

The signal receiver should avoid broadcasting every database write blindly. It can compute the possible topics for the changed instance, check Redis topic indexes, and only enqueue an `SSEModelEvent` when at least one active SSE consumer is subscribed.

An async variant may exist later for fully async producers, but the primary public API is the non-awaitable `emit_sse_event(...)`.

When publishing from database writes, application code should usually publish after commit:

```python
transaction.on_commit(
    lambda: emit_sse_event(
        ReportPDFEvent(task_id=task.id, status=task.status),
        topics={f"pdf-task:{task.id}"},
    )
)
```

## Consumer and page identity

A unique SSE consumer should be registered when each component is rendered, not only when the browser opens the `EventSource`.

Suggested stable component consumer identity:

```text
consumer_id = signed/hash(session_id, page_id, component_id, "sse")
```

The browser must not open one SSE connection per component. Instead, each browser page gets one `page_id`, and all SSE-enabled component consumers rendered into that page are attached to that page.

Suggested stable page identity:

```text
page_id = signed/random-id-created-with-the-djhtmx-repository
```

This lets the framework queue events that happen after the HTML render but before the browser connects to the page-level SSE endpoint.

If a component consumer cannot be identified, the client may create a per-component DOM-lifetime consumer ID. However, normal djhtmx components should have enough information for a stable consumer ID: the signed djhtmx session, page ID, and component ID.

Because `EventSource` cannot send custom headers, the SSE URL must include signed query parameters instead of relying on HTMX headers. That URL is page-level, not component-level:

```html
<meta
  name="djhtmx-sse-url"
  content="/_htmx/_sse/connect?session=...&page=...">
```

Each SSE-enabled component only needs to expose its consumer metadata:

```html
<div
  id="..."
  data-djhtmx-sse-consumer="...">
</div>
```

## SSE endpoint

Add one dedicated page-level SSE endpoint. It must be structurally separate from the existing component event endpoint.

Existing event endpoint:

```text
/_htmx/<app>/<component>/<component_id>/<event_handler>
```

New SSE endpoint:

```text
/_htmx/_sse/connect
```

This endpoint is not an HTMX event handler and is not produced by `{% on %}`. It is opened once per browser page by `django.js` when the page contains at least one component that provides both `sse_subscriptions` and `_handle_sse_event`.

Conceptual URL pattern:

```python
path("_sse/connect", sse_endpoint, name="djhtmx.sse")
```

Conceptual view shape:

```python
async def sse_endpoint(request: HttpRequest):
    ...
```

The client URL must include signed query parameters because `EventSource` cannot send custom HTMX headers:

```text
/_htmx/_sse/connect?session=<signed-session>&page=<signed-page>
```

Query parameters:

- `session`: signed djhtmx session ID, equivalent to the current `HX-Session` header.
- `page`: signed stable page ID that owns the currently rendered component consumers.

Request characteristics:

- method: `GET`;
- response content type: `text/event-stream`;
- cache control: `no-cache`;
- connection: one long-lived streaming response per browser page;
- CSRF: not required, because this is a read/subscribe endpoint and all mutable work happens through signed page/session state and server-side published events.

The endpoint should:

- be async;
- require ASGI and fail clearly under WSGI;
- validate the signed session and page information;
- load the page's active SSE component consumers from Redis;
- consume pending SSE events for all consumers attached to that page;
- load each target component from Redis state before invoking its `_handle_sse_event`;
- verify each target component is still SSE-enabled;
- turn resulting commands into one SSE message batch for the page;
- close when the page has no active SSE consumers, the page expires, or the client disconnects.

Failure modes:

- missing or invalid signed session: `400`;
- missing or invalid signed page: `400`;
- page/session mismatch: `403`;
- no SSE consumers for the page: keep alive briefly or close cleanly;
- WSGI runtime: explicit configuration/runtime error, no long-poll fallback.

## Python/Redis runtime topology

The intended runtime is one in-process SSE broker per ASGI worker process, not one Redis subscription per component and not one Redis connection per browser page. With Granian, each Granian worker can accept many browser SSE connections and maintain one extra async Redis connection dedicated to SSE wake subscriptions.

Within one ASGI worker process:

- many browser pages are represented by many async SSE response tasks;
- those tasks are registered in an in-memory map keyed by `page_id`;
- one broker task owns the Redis wake subscription for that worker;
- conceptually, the broker loop waits on `await get_next_redis_sse_events()`;
- when events arrive, the broker finds the affected `page_id` values and wakes the matching in-process page tasks;
- regular Redis reads/writes can use the existing Redis connection pool or a small async Redis pool.

Across multiple ASGI worker processes:

- each worker has its own in-process broker and Redis connection(s);
- each worker only knows about the browser pages connected to that worker;
- Redis remains the shared source of truth for page metadata, consumer metadata, topic indexes, and pending events;
- wake messages must include enough information, such as `page_id`, for every worker to decide whether it owns the live SSE connection for that page.

So the design is “one page-level SSE connection in the browser, many async page streams per Granian/ASGI worker, and one Redis wake listener per worker”. It is not “one global Python process for all clients”.

## Redis model

Use Redis as a small event broker.

Conceptual keys:

```text
sse:page:{page_id}
    session_id
    active_consumer_ids
    last_seen
    ttl

sse:consumer:{consumer_id}
    page_id
    component_id
    session_id
    subscriptions
    last_seen
    ttl

sse:topic:{topic}:consumers
    set[consumer_id]

sse:consumer:{consumer_id}:events
    pending event queue or stream

sse:page:{page_id}:wake
    notification channel or stream that wakes the single page connection
```

On component render:

1. Build/render the component normally.
2. Compute `component.sse_subscriptions` only if the component also provides `_handle_sse_event`.
3. Register or update the component consumer metadata.
4. Attach the component consumer to the current `page_id`.
5. Update reverse topic indexes.
6. Emit `data-djhtmx-sse-consumer` from `{% hx-tag %}` only if the component has full SSE opt-in.

On page render or `{% htmx-headers %}`:

1. Ensure a `page_id` exists for this browser page.
2. Emit the page-level SSE URL, for example in a meta tag or script configuration.
3. Do not open the SSE connection immediately unless the browser sees at least one `data-djhtmx-sse-consumer` element.

On `emit_sse_event(event, topics=...)`:

1. Serialize the typed event envelope.
2. Find matching component consumers from the topic indexes.
3. Enqueue the event into each matching consumer queue.
4. Publish or stream a compact wake record containing the affected `page_id` values.
5. Each worker's broker receives that wake record through `await get_next_redis_sse_events()` and wakes only the page tasks it owns locally.

## Reconnect and delivery

If Redis stores per-consumer pending events, `Last-Event-ID` is not strictly required for reconnect without refreshing the page.

However, SSE messages should still include an `id`:

```text
id: 1712345678-0
event: djhtmx
data: {"commands": [...]}

```

Reason: server-side “sent” is not identical to browser-side “processed”. If the connection drops after the server writes but before the browser applies the message, duplicate delivery is safer than missed delivery.

The first implementation can provide at-least-once delivery. Render commands should be treated as idempotent where possible.

## Framework-managed SSE loop

The framework, not the component, owns the loop.

Conceptual flow:

```python
while connected:
    events_by_consumer = await consume_pending_events_for_page(
        page_id,
        debounce_ms=100,
        max_batch=50,
    )

    page_commands = []

    for consumer_id, events in events_by_consumer.items():
        component_id = get_component_id_for_consumer(consumer_id)
        component = repo.get_component_by_id(component_id)

        commands = []
        needs_default_render = False

        for event in events:
            emitted = component._handle_sse_event(event)

            if emitted is None:
                needs_default_render = True
            else:
                for command in emitted:
                    if command is None:
                        needs_default_render = True
                    else:
                        commands.append(command)

        commands = coalesce_for_component(component, commands)

        if needs_default_render and not already_rendering_self(component, commands):
            commands.append(Render(component, oob="true"))

        page_commands.extend(commands)

    html = render_commands_as_oob_html(coalesce_for_page(page_commands))
    send_sse_message("djhtmx", html)
    ack_events(events_by_consumer)
```

The loop should throttle bursts and avoid unnecessary repeated full renders. Coalescing happens per component consumer first, then across the whole page batch. The final page batch is serialized as HTML/OOB fragments for the `SSEEventRouter`.

## Coalescing rules

- Multiple default renders in one burst collapse to one `Render(component)`.
- If an explicit render of the same component already exists, the default render is skipped.
- `SkipRender(self)` suppresses default rendering for ignored events.
- Explicit partial renders are preserved.
- `Destroy` commands are converted to OOB delete fragments.
- Browser commands such as `Focus`, `ScrollIntoView`, `Open`, and `DispatchDOMEvent` are deferred to the advanced command design.
- Navigation commands such as `Redirect` should either close the stream with an explicit unsupported-command error in MVP or use a later command-carrier design.

## SSE message protocol

Prefer HTML over SSE, not JSON command batches. The `SSEEventRouter` listens for one known SSE event name, `djhtmx`, and lets HTMX process the payload as a normal swap response.

Since there is one SSE connection per page, each `djhtmx` message may contain OOB fragments for several components.

Example SSE frame:

```text
event: djhtmx
data: <div id="hx-a" hx-swap-oob="true">...</div><div id="hx-b" hx-swap-oob="true">...</div>

```

Rendered HTML should be OOB by default. Browser-side routing is handled by HTMX's existing OOB swap machinery, using the DOM `id` in each returned fragment.

Supported MVP output over SSE:

- full component renders as OOB HTML;
- partial renders as OOB HTML;
- `Destroy` as an OOB delete fragment;
- no-op/heartbeat messages if needed.

Advanced browser commands such as `Focus`, `ScrollIntoView`, `DispatchDOMEvent`, `Open`, `PushURL`, `ReplaceURL`, and `Redirect` need a separate design because SSE payloads cannot use HTMX response headers. Options include:

- keep those commands unsupported over SSE in the first version;
- encode commands as special OOB command elements processed by `django.js`;
- intercept `htmx:sseBeforeMessage` for JSON command batches only when a non-HTML command is present.

The first version should optimize for the polling replacement use cases, which need renders and deletion more than browser commands.

## Client integration

`{% hx-tag %}` remains the component-level integration point. A single infrastructure component owns the page-level SSE connection.

For non-SSE components, output is unchanged.

For SSE components, `hx-tag` adds component consumer metadata, not a connection URL:

```html
<div
  id="..."
  hx-headers="..."
  data-djhtmx-sse-consumer="...">
</div>
```

### Preferred browser design: `SSEEventRouter` component

Instead of adding a custom `EventSource` manager to `django.js`, provide one hidden djhtmx component that uses the HTMX SSE extension. Applications render it once in the page, usually in the base template body.

Conceptual component:

```python
class SSEEventRouter(HtmxComponent, public=False):
    _template_name = "htmx/SSEEventRouter.html"
    id: str = "djhtmx-sse-router"
    page_id: str
```

The `sse_url` can be injected by the repository/render context because it needs the signed djhtmx session and signed page ID.

Conceptual template:

```html
<div {% hx-tag %}
     hidden
     hx-ext="sse"
     sse-connect="{{ sse_url }}">
  <div sse-swap="djhtmx" hx-swap="none"></div>
</div>
```

The root element creates exactly one `EventSource` for the page. The child element listens for the `djhtmx` SSE event. `hx-swap="none"` means the child does not visibly receive response content, while HTMX still processes OOB fragments from the SSE payload.

The server can therefore send normal HTML/OOB responses over SSE:

```text
event: djhtmx
data: <div id="hx-..." hx-swap-oob="true">...</div>

```

Browser-side routing then becomes mostly automatic: HTMX processes the OOB fragments and swaps each component update into the element with the matching DOM `id`. Destroy can use the existing OOB delete form:

```html
<div id="hx-..." hx-swap-oob="delete"></div>
```

This means the first version may not need a custom browser-side SSE router at all. `django.js` may only need shared command helpers for advanced non-HTML commands, if those commands are supported over SSE.

Requirements for this approach:

- include `htmx/2.0.4/ext/sse.js` when the router is used;
- render `SSEEventRouter` at most once per page;
- use one page-level SSE endpoint behind the router's `sse-connect` URL;
- send component renders as OOB HTML by default;
- send deletions as OOB delete fragments;
- keep component consumer registration server-side when components render.

### HTMX SSE extension evaluation

The HTMX SSE extension at <https://htmx.org/extensions/sse/> has been downloaded to `src/djhtmx/static/htmx/2.0.4/ext/sse.js` from `htmx-ext-sse@2.2.4`. The downloaded file matches the published SRI hash `sha384-A986SAtodyH8eg8x8irJnYUk7i9inVQqYigD6qZ9evobksGNIXfeFvDwLSHcp31N`.

It provides:

- `hx-ext="sse"` plus `sse-connect="<url>"` to create an `EventSource`;
- `sse-swap="<message-name>"` to swap named SSE event payloads into the DOM;
- multiple named listeners from one `EventSource`, as long as listeners are the connection element or its children;
- `hx-trigger="sse:<message-name>"` to trigger regular HTTP callbacks from SSE messages;
- `sse-close="<message-name>"` to close a stream from a server message;
- extra lifecycle events such as `htmx:sseOpen`, `htmx:sseBeforeMessage`, `htmx:sseMessage`, and `htmx:sseClose`;
- reconnection logic on top of the browser's native `EventSource` reconnect behavior.

Source-code notes from `htmx-ext-sse@2.2.4`:

- The extension registers `htmx.defineExtension("sse", ...)` and stores the internal HTMX API.
- `getSelectors()` only processes `[sse-connect]`, `[data-sse-connect]`, `[sse-swap]`, and `[data-sse-swap]`.
- On `htmx:afterProcessNode`, it creates an EventSource for elements with `sse-connect`, then registers SSE listeners for that element or descendants.
- On `htmx:beforeCleanupElement`, it closes the EventSource stored in the element's internal data.
- EventSource creation goes through `htmx.createEventSource(url)`, defaulting to `new EventSource(url, {withCredentials: true})`, so it is overrideable.
- `sse-swap` splits comma-separated event names and calls `source.addEventListener(name, listener)` for each.
- Before swapping, the listener calls `htmx:sseBeforeMessage`; if that event is cancelled, the default swap is skipped.
- Default swapping runs HTMX response transforms, computes the target and swap spec, then calls `api.swap(target, content, swapSpec, {contextElement: elt})`.
- `hx-trigger="sse:name"` does not swap; it triggers the corresponding HTMX event on the element.
- Reconnection uses exponential backoff when `source.readyState === EventSource.CLOSED`, up to a retry count of `128`, with delay `retryCount * 500ms`.

Conclusion: the extension is likely the right browser abstraction for the MVP if djhtmx sends HTML/OOB payloads instead of JSON command batches.

The key observation is that the extension's default swap path calls HTMX's normal `api.swap(...)`. With a hidden listener using `hx-swap="none"`, the visible content is not inserted into the listener, but HTMX should still process OOB fragments. That gives djhtmx page-level routing for free:

```html
<div hidden hx-ext="sse" sse-connect="/_htmx/_sse/connect?session=...&page=...">
  <div sse-swap="djhtmx" hx-swap="none"></div>
</div>
```

The server sends one named event:

```text
event: djhtmx
data: <div id="hx-..." hx-swap-oob="true">...</div>

```

HTMX receives the event, invokes the normal swap machinery, applies OOB fragments, and routes each update to the matching component DOM ID.

Limitations remain:

- the extension has no catch-all listener, so djhtmx should use one known event name such as `djhtmx`;
- non-HTML commands still need a command-carrier design or `htmx:sseBeforeMessage` interception;
- a small prototype should confirm that `hx-swap="none"` plus SSE extension processing preserves OOB swaps exactly as regular HTMX responses do.

If the prototype confirms OOB behavior, do not build a custom `EventSource` manager for the first version.

## Backwards compatibility

Compatibility requirements:

- Existing components require no code changes.
- Existing templates require no code changes.
- Existing event handler URLs remain valid.
- Existing `{% on %}` behavior is unchanged.
- Existing `{% hx-tag %}` output is unchanged unless the component opts in.
- Existing Redis session data remains valid. Any SSE metadata must be additive.
- Components do not become SSE-enabled accidentally.
- WSGI applications fail clearly only when using the SSE endpoint; non-SSE djhtmx behavior remains unchanged.

## Example: model-backed todo components

The test todo app is a useful target for database-level SSE events.

`TodoItem` represents one `Item`, so it can subscribe to instance-level updates and deletes:

```python
class TodoItem(HtmxComponent):
    item: Item

    @property
    def sse_subscriptions(self):
        return {
            SSESubscription(SSEModelEvent, topic=f"todo.item.{self.item.pk}.updated"),
            SSESubscription(SSEModelEvent, topic=f"todo.item.{self.item.pk}.deleted"),
        }

    def _handle_sse_event(self, event: SSEEvent[SSEModelEvent]):
        match event.event.action:
            case "updated":
                self.__dict__.pop("item", None)
                yield None
            case "deleted":
                yield Destroy(self.id)
```

`TodoList` represents a collection, so it can subscribe to collection-level create/delete topics. On delete, it may either re-render the whole list or remove the known child component if the child ID is derivable.

```python
class TodoList(HtmxComponent):
    @property
    def sse_subscriptions(self):
        return {
            SSESubscription(SSEModelEvent, topic="todo.item.created"),
            SSESubscription(SSEModelEvent, topic="todo.item.deleted"),
        }

    def _handle_sse_event(self, event: SSEEvent[SSEModelEvent]):
        match event.event.action:
            case "created":
                yield None
            case "deleted":
                yield Destroy(f"item-{event.event.pk}")
```

If component IDs are not derivable, `TodoList` can simply `yield None` for deletion and let the full list render remove the row through OOB replacement.

## Example: PDF button

Current polling-based templates use `every 6s` refreshes while a task is running.

SSE version:

```python
class PDFButton(BasePDFButton):
    @property
    def sse_subscriptions(self):
        if self.task_id:
            return {SSESubscription(PDFTaskChanged, topic=f"pdf-task:{self.task_id}")}
        return set()

    def _handle_sse_event(self, event: SSEEvent[PDFTaskChanged]):
        self.__dict__.pop("task", None)

        match event.event.status:
            case "done" | "failed":
                yield None
            case _:
                yield SkipRender(self)
```

When the background task changes state:

```python
emit_sse_event(
    PDFTaskChanged(task_id=task.id, status=task.status),
    topics={f"pdf-task:{task.id}"},
)
```

## Example: notification toast list

```python
class NotificationsToastList(HtmxComponent):
    @property
    def sse_subscriptions(self):
        return {
            SSESubscription(
                NotificationToastChanged,
                topic=f"user:{self.user.id}:notifications",
            )
        }

    def _handle_sse_event(self, event: SSEEvent[NotificationToastChanged]):
        notification_ids = {m.id for m in self.messages}

        if notification_ids != (self.last_notification_ids or set()):
            self.last_notification_ids = notification_ids
            yield None
        else:
            yield SkipRender(self)
```

When notification-producing code creates or updates a notification entry:

```python
emit_sse_event(
    NotificationToastChanged(user_id=user.id),
    topics={f"user:{user.id}:notifications"},
)
```

## Open questions

- Should `SSESubscription` be only topic-based, or should it also include typed event filtering in Redis indexes?
- Should event serialization use the existing djhtmx JSON encoder directly, or require registered event codecs?
- What exact Redis primitive should back pending events: streams, lists, or pub/sub plus durable queues?
- How aggressive should default throttling be?
- Should stale consumers be removed only by TTL, or also by explicit browser disconnect signals?
