import importlib
import importlib.util
import pkgutil

from django.apps import apps


def autodiscover_htmx_modules():
    """
    Auto-discover HTMX modules in Django apps.

    This discovers both:
    - htmx.py files (like standard autodiscover_modules("htmx"))
    - All Python files under htmx/ directories in apps (recursively)
    """
    for app_config in apps.get_app_configs():
        if app_config.module is not None:
            module_name = f"{app_config.module.__name__}.htmx"
            spec = importlib.util.find_spec(module_name)
            if spec is not None:
                module = importlib.import_module(module_name)
                if hasattr(module, "__path__"):
                    # If it's a package, recursively walk it importing all modules and packages.
                    for info in pkgutil.walk_packages(module.__path__, prefix=module_name + "."):
                        if not info.ispkg:
                            # `walk_packages` only imports packages, not modules; we need to
                            # import them all.
                            importlib.import_module(info.name)
