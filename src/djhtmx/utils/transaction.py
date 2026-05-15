from collections.abc import Callable
from contextvars import copy_context
from typing import Any

from django.db.transaction import on_commit as django_on_commit

from .runtime import is_testing

__all__ = ("run_on_commit",)


def run_on_commit[**P](f: Callable[P, Any], *args: P.args, **kwargs: P.kwargs):
    """Run `f(*args, **kwargs)` when the current transaction commits.

    During tests, run the function immediately so code paths that normally use
    transaction hooks remain observable inside Django `TestCase` transactions.
    """
    if is_testing():
        f(*args, **kwargs)
    else:  # pragma: no cover
        context = copy_context()
        django_on_commit(lambda: context.run(f, *args, **kwargs))
