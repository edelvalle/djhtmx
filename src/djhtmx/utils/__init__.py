from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from channels.db import database_sync_to_async as db  # type: ignore

from .autodiscover import autodiscover_htmx_modules
from .hashing import compact_hash, generate_id
from .http import get_params
from .subscriptions import get_instance_subscriptions, get_model_subscriptions
from .transaction import run_on_commit

if TYPE_CHECKING:

    def db[**P, T](f: Callable[P, T]) -> Callable[P, Awaitable[T]]: ...


__all__ = (
    "autodiscover_htmx_modules",
    "compact_hash",
    "db",
    "generate_id",
    "get_fqn",
    "get_instance_subscriptions",
    "get_model_subscriptions",
    "get_params",
    "run_on_commit",
)


def get_fqn(which):
    """Return the fully-qualified name of the object's class.

    If `which` is a type, use it directly; otherwise, look at it's class.  If we
    cannot know the module of the type, nor the name, fallback to the repr of
    the type.

    """
    cls = type(which) if not isinstance(which, type) else which
    try:
        mod = cls.__module__
    except AttributeError:
        mod = ""
    try:
        name = cls.__name__
    except AttributeError:
        return repr(cls)
    else:
        return f"{mod}.{name}" if mod else name
