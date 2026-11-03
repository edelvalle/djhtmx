from django.urls import include, path

from . import views

urlpatterns = [
    path("draft", views.draft, name="testbed-draft"),
    path("board", views.board, name="testbed-board"),
    path("_htmx/", include("djhtmx.urls")),
]
