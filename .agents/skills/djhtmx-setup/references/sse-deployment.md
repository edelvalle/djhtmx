# Running SSE

Server-sent events are the only part of djhtmx with demands on the deployment.  A project that uses none of them can ignore this file.

## The server must be ASGI

The endpoint checks, and answers `501 SSE requires ASGI` under WSGI.  Nothing else about djhtmx needs ASGI, so this is usually the reason a project moves: granian, uvicorn or daphne for the app, with the rest unchanged.

Each open page holds one long-lived connection for as long as it is open.  That is a connection per tab, not per interaction, so the server's concurrency budget has to account for pages that sit open all day.

Anything in front of it must not buffer.  The response already carries `Cache-Control: no-cache` and `X-Accel-Buffering: no`, which nginx honours; a proxy that buffers anyway holds every event until it decides to flush, and the symptom is events arriving in bursts, or after a page is closed.

The endpoint is declared `transaction.non_atomic_requests`, so `ATOMIC_REQUESTS` does not wrap the stream in a transaction that would live as long as the page.  Leave it that way.

## Redis carries the wake-ups

Events are pushed onto a per-session list and announced on a per-session pub/sub channel.  The stream waits on that channel and drains whatever is pending; if a wake is lost -- a Redis disconnect, a worker restart mid-publish -- the event stays queued and goes out on the next loop.

**`DJHTMX_SSE_HEARTBEAT_TIMEOUT`** (`30`, minimum 1) caps that wait, so it is also the worst-case delay for a lost wake.  Lowering it shortens that window and costs a loop iteration per connection per interval; raising it does the opposite.  Keep it at 30 or above unless you are chasing a specific latency.

## Renders happen in a thread pool, and each thread holds a database connection

A render needs the ORM, which is synchronous, so the SSE loop hands renders to a small pool of long-lived threads.  Every one of them owns a Django database connection.

**`DJHTMX_SSE_RENDER_WORKERS`** (`8`, minimum 1) is therefore a database-connection decision as much as a throughput one: this many connections per process, on top of whatever serves requests.  Size it against the project's PostgreSQL pool and the number of processes, not against the number of open pages.

**`DJHTMX_SSE_RENDER_QUEUE_MAX`** (`0`, unbounded) is the backpressure valve.  At `0` a burst queues without limit and latency grows quietly.  Set it and a drain that arrives with that many jobs already in flight raises, and the loop logs `dropping event drain` and skips it -- the events stay queued and go out on the next pass, so the page updates late instead of the worker falling over.  A bounded queue is the better failure.

**`DJHTMX_SSE_RENDER_HEALTHCHECK_EVERY`** (`50`) -- renders per worker between `is_usable()` checks on its connection, which is how a connection broken by a database restart or a firewall idle-timeout gets closed and reopened instead of failing renders.  Lower it if you see bursts of errors after a database blip.

**`DJHTMX_SSE_RENDER_ROTATE_EVERY`** (`200`) -- renders per worker before the connection is closed and reopened.  It exists for pooled setups: a thread that never gives its connection back defers psycopg-pool's `max_lifetime` recycling indefinitely.  If the pool has a `max_lifetime`, this has to be frequent enough to let it do its job.

**`DJHTMX_SSE_REGISTER_MAX_ATTEMPTS`** (`8`, minimum 1) -- retries for the optimistic Redis transaction that registers a component's subscriptions when two callers race on the same consumer.  It takes a fast re-mount to see one; raise it only after observing exhaustion in the logs.

## What to watch

- `dropping event drain` / `dropping heartbeat drain` warnings -- the queue bound is doing its job, and the pool is undersized for the load.
- Renders failing right after a database restart -- healthcheck interval too long.
- Connection count climbing with the number of processes -- `SSE_RENDER_WORKERS` multiplied by processes, as designed; reduce one or the other.
