from datetime import timedelta

import redis
from django.conf import settings

VERSION = "2.0.3"
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
