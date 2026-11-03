from django.urls import include, path

from . import views

urlpatterns = [
    path("board", views.board, name="testbed-board"),
    path("_htmx/", include("djhtmx.urls")),
]
