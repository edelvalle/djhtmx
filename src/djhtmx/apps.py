from django.apps import AppConfig
from django.utils.module_loading import autodiscover_modules

from .utils import autodiscover_htmx_modules


class App(AppConfig):
    name = "djhtmx"
    verbose_name = "Django HTMX"

    def ready(self):
        autodiscover_modules("live")  # legacy
        autodiscover_htmx_modules()
