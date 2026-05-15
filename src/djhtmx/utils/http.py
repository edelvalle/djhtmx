from urllib.parse import urlparse

from django.http.request import HttpRequest, QueryDict

from djhtmx import json

__all__ = ("get_params",)


def get_params(obj: HttpRequest | QueryDict | str | None) -> QueryDict:
    if isinstance(obj, HttpRequest):
        is_htmx_request = json.loads(obj.META.get("HTTP_HX_REQUEST", "false"))
        if is_htmx_request:
            return QueryDict(
                urlparse(obj.META["HTTP_HX_CURRENT_URL"]).query,
                mutable=True,
            )
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
