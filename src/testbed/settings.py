"""Settings of the project that holds the components only the tests of djhtmx use."""

import os

SECRET_KEY = "testbed"  # noqa: S105
DEBUG = True
ALLOWED_HOSTS = ["*"]

DJHTMX_REDIS_URL = os.environ.get("DJHTMX_REDIS_URL", "redis://localhost/0")

INSTALLED_APPS = [
    "testbed",
    "djhtmx",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.staticfiles",
]

MIDDLEWARE = ["djhtmx.middleware"]

ROOT_URLCONF = "testbed.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "djhtmx.context.component_repo",
            ],
        },
    },
]

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}

USE_TZ = True

STATIC_URL = "/static/"
