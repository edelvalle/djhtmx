import sys

from django.conf import settings

__all__ = ("is_testing",)


def is_testing() -> bool:
    return getattr(settings, "TESTING", False) or "test" in sys.argv
