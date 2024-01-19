import contextlib

try:
    from sentry_sdk import Hub, configure_scope

    @contextlib.contextmanager
    def sentry_transaction_name(transaction_name):
        with configure_scope() as scope:
            # XXX: The docs says we should scope.transaction, but when I do
            # that, the transaction keeps the old name (URL).
            try:
                scope.transaction.name = transaction_name
            except Exception:
                scope.transaction = transaction_name
            yield

    def sentry_span(description, **tags):
        hub = Hub.current
        span = hub.start_span(description=description)
        for tag, value in tags.items():
            span.set_tag(tag, value)
        return span


except ImportError:

    @contextlib.contextmanager
    def sentry_transaction_name(transaction_name):
        yield

    @contextlib.contextmanager
    def sentry_span(description, **tags):
        yield


def sentry_request_transaction(component_name, event_handler):
    return sentry_transaction_name(f"{component_name}.{event_handler}")
