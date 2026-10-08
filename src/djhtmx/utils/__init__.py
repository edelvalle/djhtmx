from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from channels.db import database_sync_to_async as db

from .autodiscover import autodiscover_htmx_modules
from .hashing import compact_hash, generate_id
from .http import get_params
from .names import get_fqn, get_model_full_label
from .subscriptions import get_instance_subscriptions, get_model_subscriptions
from .transaction import (
    atomic_if_requested,
    has_atomic_requests,
    run_on_commit,
)

if TYPE_CHECKING:

    def db[**P, T](f: Callable[P, T]) -> Callable[P, Awaitable[T]]: ...  # type: ignore


__all__ = (
    "atomic_if_requested",
    "autodiscover_htmx_modules",
    "compact_hash",
    "db",
    "generate_id",
    "get_fqn",
    "get_instance_subscriptions",
    "get_model_full_label",
    "get_model_subscriptions",
    "get_params",
    "has_atomic_requests",
    "run_on_commit",
)
