import sys

from django.conf import settings


def is_testing() -> bool:
    return getattr(settings, "TESTING", False) or "test" in sys.argv
