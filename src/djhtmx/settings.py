from datetime import timedelta

import redis
from django.conf import settings

VERSION = "2.0.4"
DEBUG = settings.DEBUG
CSRF_HEADER_NAME = settings.CSRF_HEADER_NAME[5:].replace("_", "-")
LOGIN_URL = settings.LOGIN_URL

SCRIPT_URLS = [
    f"htmx/{VERSION}/htmx{'' if DEBUG else '.min'}.js",
    "htmx/django.js",
]

DEFAULT_LAZY_TEMPLATE = getattr(settings, "DJHTMX_DEFAULT_LAZY_TEMPLATE", "htmx/lazy.html")
conn = redis.from_url(getattr(settings, "DJHTMX_REDIS_URL", "redis://localhost/0"))
SESSION_TTL = getattr(settings, "DJHTMX_SESSION_TTL", 3600)
if isinstance(SESSION_TTL, timedelta):
    SESSION_TTL = int(SESSION_TTL.total_seconds())


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
