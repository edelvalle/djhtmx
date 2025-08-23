import typing as t
from urllib.parse import urlparse

import mmh3
from channels.db import database_sync_to_async as db  # type: ignore
from django.db import models
from django.http.request import HttpRequest, QueryDict
from uuid6 import uuid7

from . import json

if t.TYPE_CHECKING:
    T = t.TypeVar("T")
    P = t.ParamSpec("P")

    def db(f: t.Callable[P, T]) -> t.Callable[P, t.Awaitable[T]]: ...  # noqa: UP047


def get_params(obj: HttpRequest | QueryDict | str | None) -> QueryDict:
    if isinstance(obj, HttpRequest):
        is_htmx_request = json.loads(obj.META.get("HTTP_HX_REQUEST", "false"))
        if is_htmx_request:
            return QueryDict(
                urlparse(obj.META["HTTP_HX_CURRENT_URL"]).query,
                mutable=True,
            )
        else:
            qd = QueryDict(None, mutable=True)
            qd.update(obj.GET)
            return qd
    elif isinstance(obj, QueryDict):
        qd = QueryDict(None, mutable=True)
        qd.update(obj)  # type: ignore
        return qd
    elif isinstance(obj, str):
        return QueryDict(
            query_string=urlparse(obj).query if obj else None,
            mutable=True,
        )
    else:
        return QueryDict(None, mutable=True)


def get_instance_subscriptions(
    obj: models.Model,
    actions: t.Sequence[str] = ("created", "updated", "deleted"),
):
    """Get the subscriptions to actions of a single instance of a model.

    This won't return model-level subscriptions.

    The `actions` is the set of actions to subscribe to, including any possible relation (e.g
    'users.deleted').  If actions is empty, return only instance-level subscription.

    """
    cls = type(obj)
    app = cls._meta.app_label
    name = cls._meta.model_name
    prefix = f"{app}.{name}.{obj.pk}"
    if not actions:
        return {prefix}
    else:
        return {f"{prefix}.{action}" for action in actions}


def get_model_subscriptions(
    obj: type[models.Model] | models.Model,
    actions: t.Sequence[str | None] = (),
) -> set[str]:
    """Get the subscriptions to actions of the model.

    If the `obj` is an instance of the model, return all the subscriptions
    from actions.  If `obj` is just the model class, return the top-level
    subscription.

    The `actions` is the set of actions to subscribe to, including any
    possible relation (e.g 'users.deleted').

    """
    actions = actions or (None,)
    if isinstance(obj, models.Model):
        cls = type(obj)
        instance = obj
    else:
        cls = obj
        instance = None
    app = cls._meta.app_label
    name = cls._meta.model_name
    model_prefix = f"{app}.{name}"
    prefix = f"{model_prefix}.{instance.pk}" if instance else model_prefix
    result = {(f"{prefix}.{action}" if action else prefix) for action in actions}
    return result


def generate_id():
    return f"hx-{uuid7().hex}"


def compact_hash(value: str) -> str:
    """Return a SHA1 using a base with 64+ symbols"""
    # this returns a signed 32 bit number, we convert it to unsigned with `& 0xffffffff`
    hashed_value = mmh3.hash(value) & 0xFFFFFFFF

    # Convert the integer to the custom base
    base_len = len(_BASE)
    encoded = []
    while hashed_value > 0:
        hashed_value, rem = divmod(hashed_value, base_len)
        encoded.append(_BASE[rem])

    return "".join(encoded)


# The order of the base is random so that it doesn't match anything out there.
# The symbols are chosen to avoid extra encoding in the URL and HTML, and
# allowed in plain CSS selectors.
_BASE = "ZmBeUHhTgusXNW_Y1b05KPiFcQJD86joqnIRE7Lfkrdp3AOMCvltSwzVG9yxa42"
