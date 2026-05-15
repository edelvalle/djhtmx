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


SSE_HEARTBEAT_MAX_TIME = getattr(settings, "DJHTMX_SSE_HEARTBEAT_MAX_TIME", 60)
if not isinstance(SSE_HEARTBEAT_MAX_TIME, int) or SSE_HEARTBEAT_MAX_TIME <= 0:
    raise ValueError("DJHTMX_SSE_HEARTBEAT_MAX_TIME must be a strictly positive int")


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
