import typing as t

from asgiref.sync import iscoroutinefunction
from django.http import HttpRequest, HttpResponse
from django.utils.decorators import sync_and_async_middleware


@sync_and_async_middleware
def middleware(get_response: t.Callable[[HttpRequest], HttpResponse]):
    # One-time configuration and initialization goes here.
    if iscoroutinefunction(get_response):

        async def middleware(request: HttpRequest):  # type: ignore
            response = await get_response(request)
            if repo := getattr(request, "htmx_repo", None):
                await repo.session.aflush()
                delattr(request, "htmx_repo")
            return response

    else:

        def middleware(request):
            response = get_response(request)
            if repo := getattr(request, "htmx_repo", None):
                repo.session.flush()
                delattr(request, "htmx_repo")
            return response

    return middleware
