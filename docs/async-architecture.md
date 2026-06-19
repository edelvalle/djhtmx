# Async architecture

As of 2.0, djhtmx's HTTP, SSE, and WebSocket entry points are **`async` views**,
but every dispatch they trigger runs as a **single synchronous job on a bounded
worker pool**. This split is deliberate and load-bearing: it is what keeps the
Postgres connection count bounded regardless of concurrency. This note explains
it and the one rule that makes it work.

## Two paths, one component model

| Path | Entry point | View | Dispatch |
| --- | --- | --- | --- |
| Initial full-page render | your Django view + `{% htmx %}` template tags | sync or async | synchronous, inline |
| HTMX event dispatch | `endpoint` (HTTP) | **async** | synchronous, on the pool |
| SSE stream + drains | `sse_endpoint` | **async** | synchronous, on the pool |
| WebSocket | `Consumer` (Channels) | **async** | synchronous, on the pool |

The same components serve all of them. A component never declares which path it
runs on. The repository, session, and SSE APIs are **synchronous** — there is a
single implementation of `build` / `render_html` / `get_state` / `flush` /
`register_component` / `unregister_consumer` / `dispatch_event`, used everywhere.

### Why the views are async but the work is sync

The endpoints are `async def` so they can serve SSE (a long-lived async
streaming body) and run under ASGI without tying up a Django worker thread per
connection. But Django's ORM is synchronous and **a Django DB connection is
owned per execution context** (`django.db.connections` is an
`asgiref.local.Local`, keyed per async task). If the dispatch touched the ORM on
the event loop, every in-flight request — and every *idle* SSE stream that had
resolved its user — would pin its own connection, and the connection count would
scale with concurrency until `max_connections` is exhausted.

So the views do no ORM themselves. They hand the entire dispatch to the
sync-work pool:

```
async def endpoint(...):
    return await submit_sync_work(_dispatch_request, request, ...)
```

`_dispatch_request` builds the `Repository`, runs `CommandProcessor.process`
(the synchronous command loop), renders, flushes the session, and builds the
HTTP response — all on **one pool thread that owns one DB connection**. The SSE
stream stays on the loop (its pub/sub is `redis.asyncio`), but every *drain*
(the part that builds components and renders) is likewise submitted to the pool
as one job. The user is resolved on the pool too (`submit_sync_work(get_user)`),
never via `request.auser()` on the loop.

## Event handlers: sync, async, and the bus

Handlers may be plain `def`, `async def`, or `async def` generators that
`yield` commands. The synchronous dispatcher adapts each at its dispatch point
(`CommandProcessor._invoke_handler`):

- plain `def` / sync generator → run **directly** on the pool thread (with
  `transaction.atomic` per `ATOMIC_REQUESTS` database);
- `async def` → run via `async_to_sync` on the pool thread;
- `async def` + `yield` → drained with `async for` under the same
  `async_to_sync` bridge.

`async_to_sync` makes the pool thread the *thread-sensitive* thread, so any
Django async ORM an async handler awaits (`await Model.objects.aget(...)`) runs
on that same pool thread and shares its one connection. Async handlers thus stay
within the connection budget too — they just don't get event-loop concurrency
(they occupy a pool slot for their duration).

**Sync and async handlers interoperate freely**, including across a single event
cascade, because handlers never call each other directly — they communicate
through the command/event bus (`Emit`/`Signal` → `_handle_event`). A sync handler
can emit an event that wakes an `async def _handle_event` on another component
and vice-versa.

## The pool is the connection budget

Django's ORM is synchronous and a connection is owned per thread, so the only
way to bound the connections djhtmx opens is to bound the threads that run its
work — the sync-work pool (a `ThreadPoolExecutor`, each worker holding one
long-lived connection).

```
DJHTMX_SYNC_WORKERS = the number of pool threads = djhtmx's DB connection budget
```

`DJHTMX_SSE_RENDER_WORKERS` is the former name (when the pool served only SSE
renders) and is still honoured as a deprecated alias.

Everything that touches the ORM runs on this pool: full HTTP/SSE/WebSocket
dispatches, component construction, Model-field resolution (pk→instance, during
`build`), template rendering, and the session flush. Redis pub/sub for the SSE
stream is the only I/O that runs on the event loop, and it is not a Postgres
connection.

### Connection accounting

**Peak Postgres connections ≈ `DJHTMX_SYNC_WORKERS` per process**, flat under
request and SSE-stream concurrency. An idle SSE stream holds **zero** Postgres
connections — it only borrows a pool connection while a drain is actively
rendering. Size the deployment so that `DJHTMX_SYNC_WORKERS × processes` stays
under Postgres `max_connections` (and any pgbouncer pool).

Because async handlers run via `async_to_sync` on the pool thread, even ORM a
user writes inside an `async def` handler is counted against the pool — there is
no separate "async-ORM connection" that can grow independently.

## No pool re-entrancy

The dispatch job runs the synchronous pipeline straight through; it never
re-submits to the pool. So an SSE drain occupying one pool slot does not wait on
another pool slot, and concurrent drains beyond `DJHTMX_SYNC_WORKERS` simply
queue and run as threads free up. (An earlier async-pipeline design ran the
drain on the loop and offloaded each ORM hop one level deep to avoid exactly this
re-entrancy; the single-job-on-the-pool model makes that unnecessary.)

## Validating it

`src/tests/loadtest_async.py` (`make loadtest N=40 WORKERS=4`) fires N concurrent
SSE drains through a deliberately small pool and asserts (a) no deadlock — all
drains return within a timeout — and (b) peak live pool threads ≤ the configured
size. For an end-to-end connection-bound check, run under granian + Postgres and
sample `pg_stat_activity` while opening many SSE streams plus concurrent
dispatches: the connection count should plateau at ≈ `DJHTMX_SYNC_WORKERS` and
drop back to baseline when the streams close.
