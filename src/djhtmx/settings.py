import redis
from django.conf import settings

VERSION = "2.0.2"
DEBUG = settings.DEBUG
CSRF_HEADER_NAME = settings.CSRF_HEADER_NAME[5:].replace("_", "-")
LOGIN_URL = settings.LOGIN_URL

SCRIPT_URLS = [
    f"htmx/{VERSION}/htmx{'' if DEBUG else '.min'}.js",
    f"htmx/{VERSION}/ext/ws.js",
    "htmx/django.js",
]

conn = redis.from_url(getattr(settings, "DJHTMX_REDIS_URL", "redis://localhost/0"))
