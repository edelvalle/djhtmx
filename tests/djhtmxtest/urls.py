from django.urls import include, path

urlpatterns = [path('__htmx/', include('djhtmx.urls'))]
