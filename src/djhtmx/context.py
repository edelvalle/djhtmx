from django.http import HttpRequest

from djhtmx.repo import Repository


def component_repo(request: HttpRequest):
    return {"htmx_repo": getattr(request, "htmx_repo", None) or Repository.from_request(request)}
