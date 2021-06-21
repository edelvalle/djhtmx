from django.apps import AppConfig
from django.utils.module_loading import autodiscover_modules

default_app_config = 'djhtmx.App'


class App(AppConfig):
    name = 'djhtmx'
    verbose_name = 'Django HTMX'

    def ready(self):
        autodiscover_modules('live')
