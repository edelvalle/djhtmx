from urllib.parse import urlparse

from django.http.request import HttpRequest, QueryDict

from djhtmx import json


def get_params(obj: HttpRequest | QueryDict | str | None) -> QueryDict:
    if isinstance(obj, HttpRequest):
        is_htmx_request = json.loads(obj.META.get("HTTP_HX_REQUEST", "false"))
        if is_htmx_request and (current_url := obj.META.get("HTTP_HX_CURRENT_URL")):
            return QueryDict(urlparse(current_url).query, mutable=True)
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
