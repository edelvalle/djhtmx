from django.conf import settings
from django.templatetags.static import static


VERSION = "1.9.10"
DEBUG = settings.DEBUG
CSRF_HEADER_NAME = settings.CSRF_HEADER_NAME[5:].replace("_", "-")
LOGIN_URL = settings.LOGIN_URL

SCRIPT_URLS = [
    static(f"htmx/{VERSION}/htmx{'.min' if DEBUG else ''}.js"),
    static("htmx/django.js"),
]

