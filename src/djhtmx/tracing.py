import contextlib

try:
    from sentry_sdk import Hub  # pyright: ignore[reportMissingImports]

    def sentry_span(description, **tags):  # pyright: ignore[reportRedeclaration]
        hub = Hub.current
        span = hub.start_span(description=description)
        for tag, value in tags.items():
            span.set_tag(tag, value)
        return span


except ImportError:

    @contextlib.contextmanager
    def sentry_span(description, **tags):
        yield
