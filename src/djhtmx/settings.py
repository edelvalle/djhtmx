import asyncio
from asyncio.events import AbstractEventLoop
from datetime import timedelta
from functools import lru_cache

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


def async_connection():
    return _async_connection(asyncio.get_running_loop())


@lru_cache
def _async_connection(loop: AbstractEventLoop):
    from redis.asyncio import from_url

    return from_url(getattr(settings, "DJHTMX_REDIS_URL", "redis://localhost/0"))


@lru_cache
def sync_connection():
    from redis import from_url

    return from_url(getattr(settings, "DJHTMX_REDIS_URL", "redis://localhost/0"))


SESSION_TTL = getattr(settings, "DJHTMX_SESSION_TTL", 3600)
if isinstance(SESSION_TTL, timedelta):
    SESSION_TTL = int(SESSION_TTL.total_seconds())


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
