import contextlib

from django.core.signing import Signer
from django.urls import path

from . import json
from .component import Component
from .introspection import filter_parameters, parse_request_data

try:
    from sentry_sdk import configure_scope

    @contextlib.contextmanager
    def sentry_transaction(request, component_name, event_handler):
        transaction = f"HTMX {request.method} {component_name}.{event_handler}"
        with configure_scope() as scope:
            # XXX: The docs says we should scope.transaction, but when I do
            # that, the transaction keeps the old name (URL).
            try:
                scope.transaction.name = transaction
            except Exception:
                scope.transaction = transaction
            yield


except ImportError:

    @contextlib.contextmanager
    def sentry_transaction(request, component_name, event_handler):
        yield


def endpoint(request, component_name, event_handler):
    with sentry_transaction(request, component_name, event_handler):
        id = request.META.get('HTTP_HX_TARGET')
        state = request.META.get('HTTP_X_COMPONENT_STATE', '')
        state = Signer().unsign(state)
        state = json.loads(state)
        component = Component._build(component_name, request, id, state)
        handler = getattr(component, event_handler)
        handler_kwargs = parse_request_data(request)
        handler_kwargs = filter_parameters(handler, handler_kwargs)
        return handler(**handler_kwargs) or component.render()


urlpatterns = [
    path(
        '<component_name>/<event_handler>',
        endpoint,
        name='djhtmx.endpoint',
    )
]
