from django.shortcuts import redirect, render

from . import agent


def index(request):
    return render(request, "index.html", context={"title": "index"})


def todo(request):
    return render(
        request,
        "todo.html",
        context={
            "title": "todo",
            "showing": request.GET.get("showing", "all"),
            # Without a provider key there is no agent, so the chat panel is not
            # offered at all rather than offered and broken.
            "ai_enabled": agent.is_enabled(),
        },
    )


def logged_user_counter(request):
    """A page mounting a component that requires a logged-in user, and requiring nothing itself.

    The view is deliberately unprotected: what answers an anonymous visitor is the component's own
    `user` annotation, through the djhtmx middleware.

    """
    return render(request, "logged_user_counter.html", context={"title": "logged user counter"})


def redirect_to_index(request):
    return redirect("/?frombackend=1")
