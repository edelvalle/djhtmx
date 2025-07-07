import asyncio
from collections.abc import Awaitable, Callable

from asgiref.sync import sync_to_async
from django.http import HttpRequest, HttpResponse


def middleware(
    get_response: Callable[[HttpRequest], HttpResponse]
    | Callable[[HttpRequest], Awaitable[HttpResponse]],
):
    """
    Middleware function that wraps get_response and ensures the HTMX repository
    is flushed and removed from the request after handling each request. It can handle
    both sync and async get_response automatically.
    """

    if asyncio.iscoroutinefunction(get_response):
        # Async version
        async def middleware(request: HttpRequest) -> HttpResponse:  # type: ignore
            response = await get_response(request)
            if repo := getattr(request, "htmx_repo", None):
                await sync_to_async(repo.session.flush)()
                delattr(request, "htmx_repo")
            return response

    else:
        # Sync version
        def middleware(request: HttpRequest) -> HttpResponse:
            response = get_response(request)
            if repo := getattr(request, "htmx_repo", None):
                repo.session.flush()
                delattr(request, "htmx_repo")
            return response  # type: ignore

    return middleware
