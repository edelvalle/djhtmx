from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from contextvars import copy_context
from typing import Any

from django.db import connections, transaction
from django.db.transaction import on_commit as django_on_commit

from .runtime import is_testing


@contextmanager
def atomic_if_requested() -> Iterator[None]:
    """Open a transaction on each database configured with `ATOMIC_REQUESTS`.

    A database already inside an atomic block is left alone.  Entering a nested
    block there would only take a savepoint, which narrows what a failure
    undoes instead of adding protection: the failing region would roll back on
    its own while everything around it still commits.  Leaving it alone keeps
    the outermost block the one that decides, which is how `ATOMIC_REQUESTS`
    behaved when Django itself wrapped the view.

    Databases without `ATOMIC_REQUESTS` are left in autocommit, as always.

    """
    with ExitStack() as stack:
        for alias in connections:
            connection = connections[alias]
            # Django fills the key in for every database in `settings.DATABASES`,
            # but one registered at runtime by assigning into
            # `connections.settings` never goes through that defaulting and
            # arrives without it.
            atomic_requests = connection.settings_dict.get("ATOMIC_REQUESTS", False)
            if atomic_requests and not connection.in_atomic_block:
                stack.enter_context(transaction.atomic(using=alias))
        yield


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
