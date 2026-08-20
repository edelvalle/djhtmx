import logging
from collections.abc import Awaitable, Callable

from asgiref.sync import iscoroutinefunction, sync_to_async
from django.http import HttpRequest, HttpResponse

from .exceptions import LoginRequired

logger = logging.getLogger(__name__)


def middleware(
    get_response: Callable[[HttpRequest], HttpResponse]
    | Callable[[HttpRequest], Awaitable[HttpResponse]],
):
    """
    Middleware function that wraps get_response and ensures the HTMX repository
    is flushed and removed from the request after handling each request. It can handle
    both sync and async get_response automatically.

    It also answers a `LoginRequired` raised while rendering a page with a redirect to the login
    page; see `process_exception`.
    """

    if iscoroutinefunction(get_response):
        # Async version
        async def middleware(request: HttpRequest) -> HttpResponse:
            response = await get_response(request)
            if repo := getattr(request, "htmx_repo", None):
                await sync_to_async(repo.session.flush)()
                delattr(request, "htmx_repo")
            return response

    else:
        # Sync version
        def middleware(request: HttpRequest) -> HttpResponse:  # type: ignore
            response = get_response(request)
            if repo := getattr(request, "htmx_repo", None):
                repo.session.flush()
                delattr(request, "htmx_repo")
            return response  # type: ignore

    # Django looks the hook up on the middleware *instance*, which for a function middleware is the
    # closure itself, and adapts it to its always-synchronous exception stack.
    middleware.process_exception = process_exception  # type: ignore[attr-defined]
    return middleware


def process_exception(request: HttpRequest, exception: Exception) -> HttpResponse | None:
    """Turn a `LoginRequired` escaping a page render into a redirect to the login page.

    A component annotated with a non-optional `user` raises while it is being built, and a page
    that mounts one is reachable by an anonymous visitor whenever the view itself is: a login the
    view forgot to require, a session that expired, a stale tab reloaded.  Without this the visitor
    gets a 500 for what is only a missing login.  Requests to the `/_htmx/` endpoints are answered
    by `CommandProcessor.process` instead and never reach this hook.

    The redirect carries the requested page as `next`, so the visitor comes back to it after logging
    in.  Beware of the loop this can make: a login page that itself mounts a component requiring a
    logged user redirects to itself forever, hence the error log naming the component.

    """
    if isinstance(exception, LoginRequired):
        # Imported here because this module is imported from `djhtmx/__init__.py`, before the app
        # registry is ready, and `django.contrib.auth.views` pulls in the auth models.
        from django.contrib.auth.views import redirect_to_login

        logger.error(
            "HTMX component %s requires a logged user, redirecting %s to the login page",
            exception.component_name,
            request.path,
        )
        return redirect_to_login(request.get_full_path())
    else:
        return None
