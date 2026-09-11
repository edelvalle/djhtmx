from datetime import timedelta

import redis
from django.conf import settings

VERSION = "2.0.4"
DEBUG = settings.DEBUG
CSRF_HEADER_NAME = settings.CSRF_HEADER_NAME[5:].replace("_", "-")
LOGIN_URL = settings.LOGIN_URL

SCRIPT_URLS = [
    f"htmx/{VERSION}/htmx{'' if DEBUG else '.min'}.js",
    f"htmx/{VERSION}/ext/sse.js",
    "htmx/django.js",
]

DEFAULT_LAZY_TEMPLATE = getattr(settings, "DJHTMX_DEFAULT_LAZY_TEMPLATE", "htmx/lazy.html")
REDIS_URL = getattr(settings, "DJHTMX_REDIS_URL", "redis://localhost/0")
conn = redis.from_url(REDIS_URL)
SESSION_TTL = getattr(settings, "DJHTMX_SESSION_TTL", 3600)
if isinstance(SESSION_TTL, timedelta):
    SESSION_TTL = int(SESSION_TTL.total_seconds())

SESSION_REFRESH_RATE = getattr(settings, "DJHTMX_SESSION_REFRESH_RATE", 0.5)
if not 0 <= SESSION_REFRESH_RATE <= 1:
    raise ValueError("DJHTMX_SESSION_REFRESH_RATE must be between 0 and 1")
SESSION_REFRESH_INTERVAL = int(SESSION_TTL * SESSION_REFRESH_RATE)


ENABLE_SENTRY_TRACING = getattr(settings, "DJHTMX_ENABLE_SENTRY_TRACING", True)
ENABLE_LOGFIRE_TRACING = getattr(settings, "DJHTMX_ENABLE_LOGFIRE_TRACING", False)


STRICT_EVENT_HANDLER_CONSISTENCY_CHECK = getattr(
    settings,
    "DJHTMX_STRICT_EVENT_HANDLER_CONSISTENCY_CHECK",
    False,
)

KEY_SIZE_ERROR_THRESHOLD = getattr(
    settings,
    "DJHTMX_KEY_SIZE_ERROR_THRESHOLD",
    0,
)
KEY_SIZE_WARN_THRESHOLD = getattr(
    settings,
    "DJHTMX_KEY_SIZE_WARN_THRESHOLD",
    50 * 1024,  # 50kb
)
KEY_SIZE_SAMPLE_PROB = getattr(
    settings,
    "DJHTMX_KEY_SIZE_SAMPLE_PROB",
    0.1,
)

STRICT_PUBLIC_BASE = getattr(settings, "DJHTMX_STRICT_PUBLIC_BASE", False)


# Sync-work executor: a small pool of long-lived worker threads owns one
# Django DB connection each.  ALL synchronous, ORM-touching work — auto-wrapped
# sync event handlers on the async HTTP path, component rendering, and SSE
# renders — funnels through this single pool, so the process-wide Django DB
# connection count is bounded by `SYNC_WORKERS` regardless of how many
# concurrent HTTP requests or SSE streams are in flight.  Size this in
# coordination with the host project's PG pool budget.
#
# `DJHTMX_SSE_RENDER_WORKERS` is the former name (when the pool only served the
# SSE path) and is still honoured as a deprecated alias.
SYNC_WORKERS = getattr(
    settings,
    "DJHTMX_SYNC_WORKERS",
    getattr(settings, "DJHTMX_SSE_RENDER_WORKERS", 8),
)
if SYNC_WORKERS < 1:
    raise ValueError("DJHTMX_SYNC_WORKERS must be >= 1")

# Deprecated alias kept so existing references (and any in-tree imports) resolve.
SSE_RENDER_WORKERS = SYNC_WORKERS

# 0 = unbounded queue; otherwise the executor raises when this many jobs are
# already in flight, so the SSE loop logs and drops rather than blowing up.
SSE_RENDER_QUEUE_MAX = getattr(settings, "DJHTMX_SSE_RENDER_QUEUE_MAX", 0)

# Render calls per worker between explicit `is_usable()` checks on the DB
# connection.  Catches half-broken connections without paying the cost on
# every render.
SSE_RENDER_HEALTHCHECK_EVERY = getattr(settings, "DJHTMX_SSE_RENDER_HEALTHCHECK_EVERY", 50)

# Render calls per worker before closing and rotating the DB connection.
# Interacts with psycopg-pool's `max_lifetime`: a persistent worker would
# otherwise keep its connection checked out indefinitely and defer the pool's
# scheduled recycle.  Rotation returns the connection so the pool can retire
# aged-out entries.
SSE_RENDER_ROTATE_EVERY = getattr(settings, "DJHTMX_SSE_RENDER_ROTATE_EVERY", 200)


# Upper bound on the SSE loop's `pubsub.get_message` wait between drains.  When
# heartbeat subscriptions are active, the next due tick further shortens this;
# when none are scheduled, this value is the wait.  It also caps the recovery
# window if a Redis pub/sub wake is lost: pending events are still drained on
# the next iteration, so worst-case delay is roughly this timeout.
SSE_HEARTBEAT_TIMEOUT = getattr(settings, "DJHTMX_SSE_HEARTBEAT_TIMEOUT", 30)
if SSE_HEARTBEAT_TIMEOUT < 1:
    raise ValueError("SSE_HEARTBEAT_TIMEOUT must be >= 1; preferably >= 30")


# Max WATCH/EXEC attempts for `register_component`'s optimistic transaction
# when two callers race on the same consumer's indexes set.  Concurrent
# register on the same component is unusual (it takes a fast re-mount), so a
# small budget is plenty; bump it only if you actually observe exhaustion.
SSE_REGISTER_MAX_ATTEMPTS: int = getattr(settings, "DJHTMX_SSE_REGISTER_MAX_ATTEMPTS", 8)
if SSE_REGISTER_MAX_ATTEMPTS < 1 or not isinstance(SSE_REGISTER_MAX_ATTEMPTS, int):
    raise ValueError("DJHTMX_SSE_REGISTER_MAX_ATTEMPTS must be >= 1")
