# djhtmx SSE: generalized worker and unified command pipeline

## Status

Plan. Combines two converging refactors:

1. **Render executor** — bound PG connection count to a small worker
   pool instead of growing with SSE stream count.
2. **Command generalization** — let SSE handlers emit the full djhtmx
   command graph (including `Emit`) through the same processor HTTP
   uses, eliminating the duplicate command-loop in `sse.py`.

These are two phases of the same architectural arc. Phase 1 stabilizes
the sync/async boundary in SSE so Phase 2 can change what runs on the
sync side without disturbing how it gets there.

The acute connection-leak symptom (Sentry KAIKO-JH) is already mitigated
in production by a per-render `connections.close()` in
`_render_consumer_sse_events`'s `finally` (djhtmx `sse-db-issues`
branch). Phase 1 replaces that stopgap with the persistent-worker
design rather than introducing it from scratch. There is no need for a
staged rollout or compatibility flag: the executor pool ships as the
single behavior, and the stopgap is removed in the same change.

## Progress

- [x] Phase 1 — SSE render executor (commit `61210cf`, deployed to
  staging)
- [x] Phase 2.1 — Extract `CommandProcessor` from `Repository`
  (commit `2e8e673`)
- [x] Phase 2.5 — Transport-neutral `CommandBatch` and HTTP serializer
- [x] Phase 2.2 — Command type split (`Command` / `InternalCommand` /
  `ProcessedCommand`), `HandleSSEEvents`, command docstrings
- [ ] Phase 2.3 — Normalize handler return semantics
- [ ] Phase 2.4 — Replace SSE ad-hoc command loop with `CommandProcessor`
- [ ] Phase 2.6 — Generalize the SSE command sink
- [ ] Phase 2.7 — Unify browser-side command execution

## Problems we are solving

### Connection-leak problem

Sentry KAIKO-JH: PostgreSQL pool exhaustion in the backend after SSE
roll-out.

Two compounding Django/ASGI properties:

- `StreamingHttpResponse` keeps the request open for the lifetime of
  the browser tab, so `request_finished` never fires and Django's
  per-request `close_old_connections()` hook never runs.
- Render work is dispatched through `sync_to_async(...,
  thread_sensitive=True)`, which pins each SSE stream to a sticky
  per-stream thread. Django connections are thread-local, so each
  sticky thread owns one `connections['default']` entry for as long as
  the SSE stream is alive.

`close_old_connections()` is not sufficient: it only closes when
`CONN_MAX_AGE` has elapsed, and the sticky thread keeps the connection
warm across heartbeats and events, so the obsolescence window never
opens.

The current stopgap closes connections in `_render_consumer_sse_events`'s
`finally`. This stops the leak but trades it for a PG handshake on
every render, on every stream. With single-Granian-worker deployments
and large SSE backlogs, this is meaningful avoidable churn.

### Command-duplication problem

The HTTP endpoint and the SSE renderer reimplement two different
command processors. The SSE loop bypasses the HTTP path's
`CommandQueue`, `_process_emited_commands`, normal `Emit` fan-out,
`Signal`, render coalescing, and exception handling. The two processors
have diverged on the set of commands they understand.

A concrete consequence: an SSE handler cannot yield
`Emit(FeedbackMessage.success(...))` to wake the page-global
`FeedbackMessages` component, even though that mechanism already exists
for HTTP handlers. Today the spec explicitly says `Emit` is ignored
from SSE handlers; that statement needs to be revised.

## Goal

One shared command pipeline used by both transports, running on a small
bounded pool of long-lived worker threads. Connection count is bounded
by pool size; `Emit` and the rest of the command graph work uniformly;
HTTP and SSE differ only at the final serialization layer.

## Non-goals

- Removing `sync_to_async` everywhere djhtmx uses it. Only the SSE
  render path is rearchitected.
- Async ORM in render code. Renders stay synchronous.
- Cross-process pooling (e.g. PgBouncer). Out of scope; orthogonal.
- Stream-affinity. Render jobs can land on any pool thread.
- Replacing `Emit`'s in-process semantics with cross-worker delivery
  (`emit_sse_event` already covers that).

## Combined architecture (target)

```
SSE async loop                                    HTTP request handler
       \                                                /
        \                                              /
         v                                            v
   loop.run_in_executor(SSE_RENDER_POOL, drain, ...)  Repository.dispatch_event(...)
                              |                            |
                              v                            v
                         drain_session_sync           Repository
                              |                            |
                              v                            v
                          Repository                CommandProcessor
                              |                            |
                              v                            v
                  CommandProcessor.process([HandleSSEEvents(...)])
                                       |
                                       v
                                  CommandQueue
                                       |
                                       v
                               ProcessedCommand stream
                                       |
                                       v
                                  CommandBatch
                                       |
                              +--------+--------+
                              v                 v
                       HTTP serializer   SSE serializer
                              |                 |
                              v                 v
                        body/headers       SSE fragments
```

The dedicated SSE render executor owns one persistent PG connection per
worker thread. The whole session-drain — handler invocation, `Emit`
fan-out, every triggered rerender — runs inside one executor dispatch
and shares the same connection.

## Phase 1 — SSE render executor

The smaller, urgent change. Lands first because it fixes a production
incident.

### Architecture

A djhtmx-owned, fixed-size `ThreadPoolExecutor` ("SSE render executor")
lives for the lifetime of the Granian worker process. Each worker
thread holds its own Django `connections['default']` entry, opened on
first use and reused for the thread's lifetime.

```python
# pseudo-code
SSE_RENDER_EXECUTOR = ThreadPoolExecutor(
    max_workers=settings.SSE_RENDER_WORKERS,
    thread_name_prefix="djhtmx-sse-render",
)

async def render_sse_event_fragments(session_id, user, conn=...):
    ...
    loop = asyncio.get_running_loop()
    rendered = await loop.run_in_executor(
        SSE_RENDER_EXECUTOR,
        _sse_render_job,
        session_id, user, metadata, envelopes,
    )
    ...
```

`_sse_render_job` is the sync callable the pool runs. In Phase 1 it
wraps today's `_render_consumer_sse_events`. In Phase 2 it becomes a
`Repository` + `CommandProcessor.process(...)` invocation. The
executor doesn't notice; only the job body changes.

The job does **not** call `connections.close()` anymore. Connections
are intentionally retained across calls on the same thread. The
existing per-render `finally` close is removed once the pool becomes
the default.

### Concurrency model

- Pool size N (configurable, default `4`). Total PG connections from
  SSE = N, regardless of stream count.
- Renders for the same stream are still serialized at the `await`
  boundary in the async loop. Two events on the same component cannot
  race on Redis state.
- Renders for *different* streams may run concurrently on different
  pool threads. They use different `session_id`s, so no cross-stream
  Redis-state collision.
- No stream-affinity needed. A render job can land on any worker
  thread; the thread doesn't care which stream it serves.

### Why a pool, not a single worker

A single render thread serializes all SSE renders process-wide. Under
broadcast events that hit N tabs simultaneously, the last tab waits
behind N renders. At 500 tabs × 50ms that is 25s tail latency — a
visible regression.

A small pool preserves concurrency, caps connection count at pool
size, and keeps the persistent-connection win. Pool size becomes a
single knob trading off connection budget against broadcast tail
latency.

### Configuration surface

```python
DJHTMX_SSE_RENDER_WORKERS = 8
DJHTMX_SSE_RENDER_QUEUE_MAX = 0          # 0 = unbounded; safe cap e.g. 1024
DJHTMX_SSE_RENDER_HEALTHCHECK_EVERY = 50 # render calls per worker
DJHTMX_SSE_RENDER_ROTATE_EVERY = 200     # render calls per worker before recycle
DJHTMX_SSE_RENDER_INLINE = False         # tests / sync fast-path
```

#### Sizing guidance

The binding constraint in the Kaiko backend is the psycopg connection
pool (see `kaiko/settings.py:495-602`). With pooling enabled
(`ENABLE_DB_POOLING_FOR_WEB_SERVICES=yes`), the pool is process-global
and `WEB_DB_POOL_MAX_SIZE` (default `10`) caps the number of PG
connections a single Granian process can hold open at any time.

For a Granian process dedicated to SSE (see "Deployment topology"
below), the budget partitions like this:

```
DJHTMX_SSE_RENDER_WORKERS ≤ WEB_DB_POOL_MAX_SIZE - headroom
```

where `headroom` covers:

- transient ORM access on the event loop thread during request setup
  (auth middleware on the SSE handshake, lazy `request.user`,
  middleware that touches the ORM);
- one slot reserved for reconnect-on-failure during a health-check
  rotation or `WEB_DB_POOL_MAX_LIFETIME` recycle;
- any signal/listener that fires synchronously around the render.

A reasonable starting point: `WEB_DB_POOL_MAX_SIZE - 2`. With the
backend default of `10`, that yields `DJHTMX_SSE_RENDER_WORKERS = 8`,
which is the suggested package default.

For deployments expecting sustained SSE traffic, raise
`WEB_DB_POOL_MAX_SIZE` on the SSE process and scale
`DJHTMX_SSE_RENDER_WORKERS` proportionally. The ceiling is whatever
Postgres `max_connections` allows across all Granian processes ×
their pool sizes.

#### Deployment topology

The recommended deployment splits Granian into two processes routed by
Caddy:

```
                     ┌────────────────────────┐
              /_sse* │ Granian (SSE)          │
   Caddy ────────────┤  WEB_DB_POOL_MAX_SIZE  │
              other  │  DJHTMX_SSE_RENDER_…   │
              ┌──────┤                        │
              │      └────────────────────────┘
              v
        ┌────────────────────────┐
        │ Granian (HTTP)         │
        │  WEB_DB_POOL_MAX_SIZE  │
        └────────────────────────┘
```

Benefits:

- The SSE process can hold long-lived PG connections without contending
  with short-lived HTTP requests for pool slots.
- The HTTP process keeps its psycopg pool tuned for short, bursty
  requests.
- Total PG budget = `granian_sse_pool + granian_http_pool`. Keep this
  under your Postgres `max_connections` slice.

The SSE-side `DJHTMX_SSE_RENDER_WORKERS` sizing is bounded by the SSE
process's `WEB_DB_POOL_MAX_SIZE` only. The HTTP process is unaffected
by anything in this plan.

### Lifecycle

**Startup**: lazy. Executor is constructed on first render. Avoids
paying the cost in worker boot for pages that never use SSE. The first
render incurs the first PG handshake on the first worker thread.

**Shutdown**: Granian sends SIGTERM. Register an `atexit` handler (and
a `signal.signal(SIGTERM, ...)` handler if running standalone) that:

1. Marks the executor as draining.
2. Allows in-flight renders to finish.
3. Calls `executor.shutdown(wait=True)`.

If the host runs ASGI lifespan, hook into the `shutdown` event
instead.

**Connection health**: long-lived connections can be killed by PG
restarts, `idle in transaction` timeouts, or DBA-initiated
`pg_terminate_backend`. Strategy:

1. Wrap the job body in a try/except for `(OperationalError,
   InterfaceError)`.
2. On failure: close the broken connection
   (`connections['default'].close()`) and re-raise. The next render on
   this thread opens a fresh connection.
3. Periodic health check: every `DJHTMX_SSE_RENDER_HEALTHCHECK_EVERY`
   renders on a thread, run `connections['default'].is_usable()` and
   close if unusable.

**Connection rotation (psycopg-pool interaction)**: when the backend
runs with psycopg pooling on, `WEB_DB_POOL_MAX_LIFETIME` (default
`1800s`) recycles pooled connections after they age out. A persistent
render worker checks out one pool connection and keeps it for many
renders, which defers the recycle indefinitely — the pool cannot
retire a connection that is still checked out. This bypasses the
lifetime safety the pool was configured for.

Mitigation: every `DJHTMX_SSE_RENDER_ROTATE_EVERY` renders on a thread
(default `200`), the worker calls `connections['default'].close()`
*before* the next render starts. The connection returns to the pool,
the pool retires it if it is past `max_lifetime`, and the next render
checks out a fresh one. The cost is one pool round-trip per N renders,
which is negligible compared to the broken-lifetime alternative.

If running without psycopg pooling, the rotation also reseats Django's
per-thread connection, which is cheap insurance against half-broken
sockets that the explicit health check misses.

**Hot-reload (dev)**: `runserver` recycles threads on auto-reload. The
executor is recreated per process; stale connections close with the
process.

### Backpressure

Each SSE stream awaits its render before issuing the next, so in-flight
queue depth is at most one-per-stream. With N pool workers and S
streams, steady-state queue depth is `max(0, S - N)`. Memory is
bounded by stream count, which the framework already bounds via TCP
backlog.

Hard cap (`DJHTMX_SSE_RENDER_QUEUE_MAX`) is defensive: if the queue
ever grows beyond the cap, new submissions raise and the SSE loop logs
an error, skips the render, and continues. Better than runaway memory.

### Tail-latency model

Worst case: a broadcast event hits S streams simultaneously. Pool
processes them at rate `pool_size / render_time`. Tail latency for the
last stream:

```
tail ≈ (S / pool_size) × render_time
```

500 streams, 50ms renders, pool of 4 → tail ≈ 6.25s. That is the cost
of bounded resources; document it and let operators tune
`DJHTMX_SSE_RENDER_WORKERS` to their broadcast pattern. Compared
against today's behavior (500 concurrent sticky threads with 500 PG
connections, pool exhausted within seconds), the trade is firmly in
favor of bounded.

### Testing

Django test client and djhtmx unit tests don't run an event loop the
way Granian does. Provide a sync fast-path:

- If no running event loop, or
  `settings.DJHTMX_SSE_RENDER_INLINE = True`: call the render job
  directly on the calling thread.
- Otherwise: dispatch through the executor.

This keeps tests fast and independent of executor lifecycle.

### Phase 1 deployment

The per-render `connections.close()` stopgap is already in production,
so this phase ships as a single backwards-incompatible change rather
than a flag-gated rollout. The release notes call out:

1. **Implement the executor** in djhtmx. The submitted callable wraps
   today's `_render_consumer_sse_events`; the per-render
   `connections.close()` is removed in the same commit since the new
   model retains connections by design.
2. **Cut a djhtmx release** with the executor as the sole behavior.
3. **Backend rolls out** the new djhtmx and the dedicated SSE Granian
   process behind Caddy `/_sse/*` routing (see "Deployment topology").
4. **Tune** `DJHTMX_SSE_RENDER_WORKERS` and `WEB_DB_POOL_MAX_SIZE` on
   the SSE process based on observed broadcast tail latency and
   sustained SSE connection count.

No staged flag, no compatibility shim. If a regression is observed,
the rollback path is to redeploy the previous djhtmx release; the
stopgap that was running before is intact in older versions.

### Phase 1 risks

- **Slow render starves a worker.** A single render that blocks (e.g.
  N+1 query against a slow table) ties up one of N workers for its
  duration. Mitigation: documentation + metrics. Not specific to this
  design; the same problem exists for HTTP handlers.
- **`contextvars` propagation.** `loop.run_in_executor(custom_pool,
  ...)` does not invoke `asgiref.sync.sync_to_async`'s context
  propagation. If any djhtmx code depends on context being inherited
  by the worker thread (Sentry scope, tracing, etc.), wrap the
  submitted callable with `contextvars.copy_context().run(...)`.
  Validate before flipping the default.
- **`request.user` lazy access.** `user` is captured in the SSE
  handler at request time; if it lazy-loads via the ORM inside the
  worker, the worker's persistent connection serves it. Verify it
  doesn't depend on thread-local request state.
- **Multi-process deployments.** PG connections scale as `processes ×
  pool_size`. Sizing should account for this.

## Phase 2 — Unified command pipeline

The larger architectural change. Lands after Phase 1 has stabilized in
production.

### Current state

Two parallel command processors:

**HTTP** (`src/djhtmx/urls.py:endpoint`):

```text
endpoint
  -> Repository.dispatch_event
      -> CommandQueue
      -> Repository._run_command
      -> Repository._process_emited_commands
  -> endpoint command match
      -> body HTML
      -> HX headers
      -> HX trigger headers
```

Supports the full command graph: `Execute`, `Emit`, `Signal`, `Render`,
`BuildAndRender`, `Destroy`, browser commands, URL commands.

**SSE** (`src/djhtmx/sse.py:_render_consumer_sse_events`):

```text
render_sse_event_fragments
  -> group Redis envelopes by consumer
  -> _render_consumer_sse_events
      -> call component._handle_sse_events directly
      -> local ad-hoc command match
```

Reimplements a smaller, divergent processor:

- supports `Render`, `BuildAndRender`, `Destroy`, `Open`;
- logs `Emit` and most browser commands as unsupported;
- bypasses `CommandQueue`, `_process_emited_commands`, `Emit` fan-out,
  `Signal`;
- separate default-render and exception handling.

`docs/plans/sse-spec.md` currently states `Emit` from SSE is permanently
unsupported. That statement is revised by this plan.

### Target shape

One processor:

```text
handler invocation
  -> CommandProcessor / CommandQueue
  -> ProcessedCommand stream
  -> CommandBatch
      -> HTTP serializer  -> body/headers/triggers
      -> SSE serializer   -> fragments + browser-command sink
```

Separation of concerns:

1. **Command semantics**: one place decides what `Emit`, `Signal`,
   `Render`, `SkipRender`, `BuildAndRender`, `Destroy`, and the
   browser commands mean.
2. **Transport encoding**: HTTP and SSE differ only in how the final
   `CommandBatch` is serialized.

`Emit` stays an in-process, session-local fan-out command. It is not
serialized over SSE. In SSE handlers, `yield Emit(...)` wakes
`_handle_event` listeners in the same session through the same
`LISTENERS` mechanism HTTP already uses. `emit_sse_event` remains the
Redis-backed cross-worker/cross-session delivery mechanism — distinct
and complementary.

### Sub-phase 2.1 — Extract `CommandProcessor`

New module: `src/djhtmx/command_processor.py`.

Move the command-loop logic currently embedded in `Repository` into a
dedicated processor:

```python
class CommandProcessor:
    def __init__(self, repo: Repository):
        self.repo = repo

    def process(self, commands: Iterable[Command]) -> Iterable[ProcessedCommand]:
        ...
```

`Repository.dispatch_event` becomes a thin wrapper:

```text
Repository.dispatch_event(...)
  -> CommandProcessor(self).process([Execute(...)])
```

`Repository.adispatch_event` keeps its current async wrapper style.

The processor owns what is currently in:

- `Repository._run_command`;
- `Repository._process_emited_commands`.

`Repository` remains responsible for:

- loading and storing components;
- building components;
- rendering HTML;
- session storage;
- query parameter patching;
- component registration.

Existing tests must pass unchanged.

### Sub-phase 2.2 — Command type split + `HandleSSEEvents`

This sub-phase tightens the command type model so that handlers can
only yield user-facing commands, then adds the SSE entry-point command.

#### 2.2.a — Three named unions in `djhtmx.commands`

```python
# Handler-yieldable.  This is the public API surface for event handlers.
Command = (
    Render | BuildAndRender | Destroy | Emit | SkipRender
    | Open | Focus | ScrollIntoView | Redirect | DispatchDOMEvent
    | PushURL | ReplaceURL
    | Execute            # server-side handler invocation; user code may chain
)

# Queue-only.  Created by the transport or by the processor itself.
# Handlers must never yield these.
InternalCommand = Signal | HandleSSEEvents

# Wire-effect subset that the transport (CommandBatch) consumes.
ProcessedCommand = (
    SendHtml | Destroy | Open | Focus | ScrollIntoView | Redirect
    | DispatchDOMEvent | PushURL | ReplaceURL
)
```

`SendHtml` is intentionally not in `Command` (handlers don't yield it)
nor in `InternalCommand` (it's never enqueued — `_run_command`
synthesizes it from `Render` and yields it out to the transport).  It
appears only in `ProcessedCommand`.

`CommandQueue` is typed `list[Command | InternalCommand]`.

`CommandProcessor.process(commands: Iterable[Command | InternalCommand])`.

`Repository.dispatch_event` / `adispatch_event` return
`Iterable[ProcessedCommand]`.

Handlers' yield type becomes `Iterable[Command | None] | None`.  The
`None`s are normalized in Phase 2.3.

Today the legacy `Command` union conflates handler-yieldable with
queue-internal (`Signal` and `Execute` are both in it).  After this
sub-phase, type-checking catches handlers that accidentally yield
`Signal`/`HandleSSEEvents` — they're no longer assignable to
`Command`.  `Execute` stays in `Command` because user code legitimately
yields it to chain into other handlers (see the comparison with
`DispatchDOMEvent`: `Execute` is server-side method dispatch,
`DispatchDOMEvent` is browser-side event fire — different planes).

#### 2.2.b — `HandleSSEEvents`

Add the new internal command:

```python
@dataclass(slots=True)
class HandleSSEEvents:
    """Internal.  Deliver a batch of SSE envelopes to a component."""
    component_id: str
    envelopes: tuple[SSEEventEnvelope[Any], ...]
```

`CommandProcessor` handles it:

```text
HandleSSEEvents
  -> load component by id
  -> call component._handle_sse_events(envelope) for each envelope
  -> normalize yielded commands
  -> enqueue them into the same CommandQueue
  -> apply the same default-render policy
  -> convert exceptions into Emit(HtmxUnhandledError(...))
```

This is the key change that enables `Emit` from SSE.

Example:

```python
def _handle_sse_events(self, envelope: SSEEventEnvelope[ReportReady]):
    yield Emit(FeedbackMessage.success("Report is ready"))
```

Pipeline:

```text
HandleSSEEvents
  -> Emit(FeedbackMessage.success(...))
  -> LISTENERS[FeedbackMessage]
  -> FeedbackMessages._handle_event(...)
  -> default Render(FeedbackMessages)
  -> SendHtml(...)
  -> SSE OOB fragment
```

`FeedbackMessages` becomes page-global for SSE-triggered notifications
with no special case.

#### 2.2.c — Docstrings

Every command dataclass gets a substantive docstring covering:

- Whether it is handler-yieldable, internal, or transport-output.
- The semantics (what effect it has).
- When to use it from application code (or that it's internal).
- Field-level notes where the meaning is non-obvious.

This addresses the long-standing "every command has a one-line
fragment of a docstring or none at all" state of `commands.py`.

### Sub-phase 2.3 — Normalize handler return semantics

Today SSE supports:

- handler returns `None` → default render;
- handler yields `None` → default render;
- handler yields `SkipRender(self)` → no default render.

HTTP does not normalize `None` commands safely. Introduce a shared
helper in `command_processor.py`:

```text
normalize_handler_commands(component, emitted, render_policy)
```

Rules:

1. If the handler returns `None`, enqueue an implicit default render.
2. If the handler yields no commands, enqueue an implicit default render.
3. If the handler yields `None`, treat it as `Render(component)`.
4. If the handler yields `SkipRender(component)`, suppress only the
   implicit default render for that handler invocation.
5. Explicit `Render`, `BuildAndRender`, `Destroy`, `Emit`, etc. are
   enqueued normally.
6. For HTTP direct event handlers, keep the current behavior where the
   direct component render uses `lazy=False`.
7. For SSE handlers and internal `Emit` handlers, keep the current
   non-direct behavior where default rendering respects `component.lazy`.

Same SSE API; behavior moves into the single processor.

### Sub-phase 2.4 — Replace SSE ad-hoc command loop

Rewrite `src/djhtmx/sse.py` so it no longer calls `_handle_sse_events`
directly.

Today:

```text
render_sse_event_fragments
  -> _render_consumer_sse_events
      -> direct _handle_sse_events call
      -> local command match
```

Target:

```text
render_sse_event_fragments
  -> load pending Redis events
  -> group/load metadata
  -> build one Repository for the SSE session
  -> build HandleSSEEvents commands
  -> CommandProcessor(repo).process(...)
  -> encode ProcessedCommand stream as SSE fragments
```

Important: process all consumers for the session in a single batch,
not one repository per consumer.

This yields:

- one `CommandQueue` per drain;
- normal render coalescing;
- normal `Emit`;
- normal `Signal`;
- normal `BuildAndRender`;
- normal exception recovery;
- one final session flush;
- better connection-level coalescing.

Heartbeats follow the same shape:

```text
render_sse_heartbeat_fragments
  -> synthesize SSEHeartbeat envelopes
  -> HandleSSEEvents
  -> CommandProcessor
  -> SSE encoder
```

Delete `_render_consumer_sse_events` and `_render_open_command` once
replaced.

### Sub-phase 2.5 — Transport-neutral `CommandBatch`

Today `urls.endpoint` maps processed commands to HTTP response parts;
`sse.py` maps them to SSE fragments. Replace both with a shared
accumulator. Likely module: `src/djhtmx/command_response.py`.

```python
@dataclass
class CommandBatch:
    html: list[SafeString]
    browser_commands: list[BrowserCommand]
```

`BrowserCommand` wraps `Redirect`, `Focus`, `ScrollIntoView`, `Open`,
`DispatchDOMEvent`, `PushURL`, `ReplaceURL`. `SendHtml` and `Destroy`
become HTML fragments in the batch.

One shared collector handles `ProcessedCommand`:

```text
ProcessedCommand -> CommandBatch
```

Separate serializers handle transport:

```text
CommandBatch -> HTTP body/headers/triggers
CommandBatch -> SSE OOB fragments
```

Command matching lives in one place; only the final delivery layer
differs.

### Sub-phase 2.6 — Generalize the SSE command sink

Today the SSE sink supports only `Open`:

```html
<div id="djhtmx-sse-commands-..." data-djhtmx-sse-command-sink="..." hidden></div>
```

Generalize to a session-scoped browser command carrier. Encode commands
generically:

```html
<div hx-swap-oob="beforeend: #djhtmx-sse-commands-abc123">
  <template
    data-djhtmx-browser-command
    data-session="abc123"
    data-payload="BASE64URL_JSON_PAYLOAD">
  </template>
</div>
```

Payload example (pre-encoding):

```json
{
  "command": "scroll_into_view",
  "selector": "#item-123",
  "behavior": "smooth",
  "block": "center",
  "if_not_visible": true
}
```

Supports all browser commands and arbitrary `DispatchDOMEvent.detail`
without fragile attributes.

Browser keeps the same safety model:

- process only commands inside the matching session sink;
- require session hash match;
- allow only known command names;
- validate URLs for `open` and `redirect`;
- validate `open.target`;
- remove command element after processing.

### Sub-phase 2.7 — Unify browser-side command execution

`src/djhtmx/static/htmx/django.js` currently has three command
dialects:

1. HTMX trigger events: `hxFocus`, `hxScrollIntoView`, `hxOpenURL`,
   `hxDispatchDOMEvent`.
2. SSE sink: only `open`.
3. WebSocket JSON: `destroy`, `focus`, `scroll_into_view`, `redirect`,
   `dispatch_event`, `push_url`, etc.

Refactor JS into one executor:

```javascript
function executeBrowserCommand(commandData) {
    switch (commandData.command) {
        case "focus": ...
        case "scroll_into_view": ...
        case "open-tab": ...
        case "redirect": ...
        case "push_url": ...
        case "replace_url": ...
        case "dispatch_dom_event": ...
    }
}
```

Adapt:

- SSE command sink → decode payload → `executeBrowserCommand`;
- HTMX trigger handlers → convert event detail → `executeBrowserCommand`;
- WebSocket JSON → `executeBrowserCommand`.

Note: the current JS WebSocket switch uses `dispatch_event`, while the
Python command literal is `dispatch_dom_event`. Normalize to
`dispatch_dom_event`; optionally accept `dispatch_event` as a legacy
alias.

### Behavioral decisions made explicit

#### `Emit` from SSE

`Emit` is **always** session-local.  No opt-in cross-session republish.
The only way to reach other sessions is `emit_sse_event`.

- Wakes `_handle_event` listeners already rendered in the same djhtmx
  session.
- Does not publish Redis SSE events.
- Does not cross browser sessions.
- Processed synchronously inside the SSE command queue.
- Usable for page-global UI effects such as `FeedbackMessages`.

The distinction with `emit_sse_event` is load-bearing and intentional:

```text
Emit(...)
  -> in-process/page-session event fan-out

emit_sse_event(...)
  -> Redis-backed cross-worker/cross-session SSE event delivery
```

Mixing the two — e.g. having `Emit` optionally republish to Redis —
blurs the boundary, makes failure modes harder to reason about
(Redis-down vs in-process error), and complicates the SSE producer
contract.  Keep them separate.

#### SSE browser commands

After Phase 2.6, SSE supports all browser commands: `Focus`,
`ScrollIntoView`, `Open`, `Redirect`, `PushURL`, `ReplaceURL`,
`DispatchDOMEvent`. `Destroy` remains an OOB delete fragment because
HTMX handles it cleanly.

#### HTTP behavior unchanged externally

- `SendHtml` → response body.
- `Destroy` → OOB delete HTML.
- `Redirect` → `HX-Redirect`.
- `PushURL` → `HX-Push-Url`.
- `ReplaceURL` → `HX-Replace-Url`.
- `Focus`, `ScrollIntoView`, `Open`, `DispatchDOMEvent` →
  `HX-Trigger-After-Settle`.

Produced by the shared `CommandBatch` serializer, not by endpoint-local
matching.

#### Redirect precedence

Keep current HTTP rule:

```text
HX-Redirect removes HX-Push-Url and HX-Replace-Url
```

Define the SSE analogue in the SSE serializer or browser executor:
drop `push_url`/`replace_url` from the same batch when `redirect` is
present.

## How the phases compose

Phase 1 establishes the executor as the dispatch surface for SSE
renders. The submitted callable is opaque to the executor — in Phase 1
it wraps today's `_render_consumer_sse_events`; in Phase 2 it becomes:

```python
def _sse_render_job(session_id, user, batched_envelopes_by_consumer):
    repo = Repository(user=user, session=Session(session_id), params=get_params(None))
    commands = [
        HandleSSEEvents(component_id=consumer_to_component[c], envelopes=tuple(envs))
        for c, envs in batched_envelopes_by_consumer.items()
    ]
    processed = list(CommandProcessor(repo).process(commands))
    batch = CommandBatch.from_processed(processed)
    repo.session.flush()
    return batch.to_sse_fragments()
```

Connection management does not change between phases. The persistent
PG connection on the pool thread serves the whole drain — handler
invocation, `Emit` fan-out, every triggered rerender — for free.

This composition also simplifies the executor's interface from
"per-consumer render job" to "per-session drain job", which means fewer
dispatches per loop iteration and one Repository per drain instead of
one per consumer.

## Suggested implementation order

1. **Phase 1 — Implement render executor**. `_sse_render_job` wraps
   today's `_render_consumer_sse_events`; remove the per-render
   `connections.close()` stopgap in the same commit. Backend deploys
   the dedicated SSE Granian process behind Caddy `/_sse/*` routing.
   Ship as a single djhtmx release.
2. **Phase 2.1 — Extract `CommandProcessor`**. HTTP unchanged. Run
   existing tests.
3. **Phase 2.5 — Extract `CommandBatch` and HTTP serializer**.
   Endpoint-local matching moves out of `urls.py`.
4. **Phase 2.2 — Add `HandleSSEEvents` internal command**.
5. **Phase 2.3 — Normalize handler return semantics** in
   `command_processor`.
6. **Phase 2.4 — Rewrite SSE rendering** to use `CommandProcessor`.
   `_sse_render_job` switches body. Delete `_render_consumer_sse_events`
   and `_render_open_command`.
7. **Phase 2.6 — Generalize SSE command sink**.
8. **Phase 2.7 — Unify JS browser command execution**.
9. **Cleanup** — remove any remaining duplicated command matches and
   stale heartbeat code paths.

## Test plan

### Phase 1

- PG connection count tracks pool size, not stream count.
- Broadcast latency stays within target under N-stream / pool-size-M
  conditions.
- Tests using the sync fast-path pass without executor lifecycle.
- Connection rotation works after simulated PG restart.
- `contextvars`-dependent code (Sentry scope, tracing) sees the
  expected context inside the worker thread.

### Phase 2

#### Command processor

- HTTP direct handler still renders the component by default.
- Handler yielding `SkipRender(self)` suppresses default render.
- Handler yielding `None` results in default render.
- `Emit` wakes `_handle_event` listeners and renders them.
- `BuildAndRender` registers parent/child relationship.
- Multiple `Render` for the same component coalesce.

#### HTTP serializer

Adapt existing `src/tests/test_urls.py`:

- `Destroy` creates OOB delete HTML.
- `Redirect` sets `HX-Redirect`.
- `Focus`, `ScrollIntoView`, `Open`, `DispatchDOMEvent` become trigger
  headers.
- `PushURL`, `ReplaceURL` headers still work.
- `Redirect` still suppresses push/replace headers.

#### SSE processor

Tests where an SSE handler yields each of: `Render(self)`, `None`,
`SkipRender(self)`, `Destroy(...)`, `BuildAndRender(...)`, `Open(...)`,
`Focus(...)`, `ScrollIntoView(...)`, `Redirect(...)`, `PushURL(...)`,
`ReplaceURL(...)`, `DispatchDOMEvent(...)`,
`Emit(FeedbackMessage.success(...))`.

The key regression:

```text
SSE handler yields Emit(FeedbackMessage.success(...))
  -> FeedbackMessages._handle_event receives it
  -> SSE response contains OOB render for FeedbackMessages
```

#### Browser-side / manual

- SSE `Open` still works.
- SSE `Focus` focuses the element.
- SSE `ScrollIntoView` scrolls.
- SSE `Redirect` navigates.
- SSE `PushURL` and `ReplaceURL` update history.
- SSE `DispatchDOMEvent` dispatches with expected `detail`.
- Commands for the wrong session are ignored and removed.

### Project checks

```bash
make test
make lint
make pyright
```

## Out of scope (follow-ups)

- Async-native ORM port for SSE renders. Django's async ORM coverage is
  still partial.
- Per-tenant connection pools.
- Replacing `sync_to_async` elsewhere in djhtmx (consumer registration,
  Redis sync ops). Those are short-lived and not hot paths.

## References

- `src/djhtmx/sse.py` — render dispatch sites at
  `render_sse_event_fragments`, `render_sse_heartbeat_fragments`,
  `_render_consumer_sse_events`.
- `src/djhtmx/urls.py:118-222` — `sse_endpoint` async loop.
- `src/djhtmx/repo.py` — `Repository.dispatch_event`,
  `_run_command`, `_process_emited_commands`.
- `src/djhtmx/static/htmx/django.js` — current JS command dialects.
- `docs/plans/sse-spec.md` — current spec (update Phase 2 to revise
  the "Emit is unsupported" statement and the Open-only sink).
- Sentry KAIKO-JH — original incident.
- GitHub PR https://github.com/edelvalle/djhtmx/pull/54 — initial SSE
  implementation.

## Summary

```text
Phase 1: bind PG connections to a small pool of long-lived worker
threads; render jobs dispatch through this pool.

Phase 2: collapse HTTP and SSE command paths into one CommandProcessor;
the SSE render job becomes a CommandProcessor invocation, picking up
Emit, Signal, coalescing, and the full browser command set for free.

Composed: the entire SSE drain — handler, Emit fan-out, every triggered
rerender — runs in one executor dispatch on one persistent PG
connection. Connection count is bounded by pool size; commands work
uniformly; HTTP and SSE only differ at the final serialization layer.
```
