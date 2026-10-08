from django.shortcuts import redirect, render


def index(request):
    return render(request, "index.html", context={"title": "index"})


def todo(request):
    return render(
        request,
        "todo.html",
        context={
            "title": "todo",
            "showing": request.GET.get("showing", "all"),
        },
    )


def logged_user_counter(request):
    """A page mounting a component that requires a logged-in user, and requiring nothing itself.

    The view is deliberately unprotected: what answers an anonymous visitor is the component's own
    `user` annotation, through the djhtmx middleware.

    """
    return render(request, "logged_user_counter.html", context={"title": "logged user counter"})


def board(request):
    return render(request, "board.html", context={"title": "board"})


def redirect_to_index(request):
    return redirect("/?frombackend=1")
