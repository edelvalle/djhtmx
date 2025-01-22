import contextlib

# pragma: no cover

try:
    import sentry_sdk  # pyright: ignore[reportMissingImports]

    @contextlib.contextmanager
    def sentry_span(name: str, **tags: str):  # pyright: ignore[reportRedeclaration]
        with sentry_sdk.start_span(op="djhtmx", name=name) as span:
            for tag, value in tags.items():
                span.set_tag(tag, value)
            yield span


except ImportError:

    @contextlib.contextmanager
    def sentry_span(description, **tags):
        yield
