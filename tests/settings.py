import os

DIRNAME = os.path.dirname(__file__)

DEBUG = True
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": "memory:///",
    }
}
DEFAULT_AUTO_FIELD = "django.db.models.AutoField"

INSTALLED_APPS = (
    "django.contrib.staticfiles",
    "djhtmx",
)

STATIC_URL = "/static/"
SECRET_KEY = "abc123"
MIDDLEWARE = []

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

ROOT_URLCONF = "djhtmxtest"

# Swappable model testing
MPTT_SWAPPABLE_MODEL = "myapp.SwappedInModel"
