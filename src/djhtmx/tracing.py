import contextlib

from djhtmx import settings

# pragma: no cover

try:
    import sentry_sdk  # pyright: ignore[reportMissingImports]
except ImportError:
    sentry_sdk = None


if settings.ENABLE_SENTRY_TRACING and sentry_sdk is not None:
    sentry_start_span = sentry_sdk.start_span

    @contextlib.contextmanager
    def _sentry_span(name: str, **tags: str):  # pyright: ignore[reportRedeclaration]
        with sentry_start_span(op="djhtmx", name=name) as span:
            for tag, value in tags.items():
                span.set_tag(tag, value)
            yield span

else:

    @contextlib.contextmanager
    def _sentry_span(description, **tags):
        yield


try:
    import logfire  # pyright: ignore[reportMissingImports]
except ImportError:
    logfire = None


if settings.ENABLE_LOGFIRE_TRACING and logfire is not None:
    logfire_span = logfire.span

    def _logfire_span(name: str, **tags):  # pyright: ignore[reportRedeclaration]
        return logfire_span(name, op="djhtmx", **tags)

else:

    @contextlib.contextmanager
    def _logfire_span(description, **tags):
        yield


@contextlib.contextmanager
def tracing_span(name: str, **tags: str):
    with _sentry_span(name, **tags), _logfire_span(name, **tags):
        yield
