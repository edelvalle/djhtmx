import contextlib
from collections.abc import Mapping

from djhtmx import settings

# pragma: no cover

try:
    import sentry_sdk  # pyright: ignore[reportMissingImports]
except ImportError:
    sentry_sdk = None


@contextlib.contextmanager
def sentry_tags(**tags: str):
    if sentry_sdk is None or not tags:
        yield
        return

    with sentry_sdk.push_scope() as scope:
        for tag, value in tags.items():
            scope.set_tag(tag, value)
        yield scope


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


# Metrics: thin dual-backend wrapper.  Each helper publishes to Sentry's Trace
# Metrics API (when sentry-sdk is installed and Sentry tracing is on) and to
# Logfire's metric instruments (when logfire is installed and Logfire tracing
# is on).  Helpers are no-ops if neither backend is enabled.  Sentry's old DDM
# metrics (`sentry_sdk.metrics.incr`) were removed in 2.41; we require >=2.62
# and use the replacement `count` / `distribution` Trace Metrics API.


def metric_incr(name: str, value: int = 1) -> None:
    """Counter increment: a monotonically increasing per-process tally."""
    if _sentry_metric_count is not None:
        _sentry_metric_count(name, value)
    if _logfire_metric_counter is not None:
        instrument = _logfire_counters.get(name)
        if instrument is None:
            instrument = _logfire_metric_counter(name)
            _logfire_counters[name] = instrument
        instrument.add(value)  # type: ignore[attr-defined]


def metric_distribution(name: str, value: float) -> None:
    """Distribution / histogram sample.  Used for durations and queue waits."""
    if _sentry_metric_distribution is not None:
        _sentry_metric_distribution(name, value)
    if _logfire_metric_histogram is not None:
        instrument = _logfire_histograms.get(name)
        if instrument is None:
            instrument = _logfire_metric_histogram(name)
            _logfire_histograms[name] = instrument
        instrument.record(value)  # type: ignore[attr-defined]


def htmx_headers_as_tags(headers: Mapping[str, object]) -> dict[str, str]:
    return {
        tag: value
        for header, tag in SAFE_HTMX_REQUEST_HEADERS.items()
        if (value := _get_header_value(headers, header)) not in (None, "")
    }


if settings.ENABLE_SENTRY_TRACING and sentry_sdk is not None:
    _sentry_metric_count = sentry_sdk.metrics.count
    _sentry_metric_distribution = sentry_sdk.metrics.distribution
else:
    _sentry_metric_count = None
    _sentry_metric_distribution = None


if settings.ENABLE_LOGFIRE_TRACING and logfire is not None:
    _logfire_metric_counter = getattr(logfire, "metric_counter", None)
    _logfire_metric_histogram = getattr(logfire, "metric_histogram", None)
else:
    _logfire_metric_counter = None
    _logfire_metric_histogram = None


# Caches: Logfire's metric_* factories return instrument objects that must be
# reused; recreating them on every call would double-register metrics.
_logfire_counters: dict[str, object] = {}
_logfire_histograms: dict[str, object] = {}


def _get_header_value(headers: Mapping[str, object], header: str) -> str | None:
    if (value := headers.get(header)) not in (None, ""):
        return str(value)

    value = headers.get(f"HTTP_{header.upper().replace('-', '_')}")
    if value in (None, ""):
        return None
    return str(value)


SAFE_HTMX_REQUEST_HEADERS = {
    "HX-Boosted": "hx-boosted",
    "HX-Current-URL": "hx-current-url",
    "HX-History-Restore-Request": "hx-history-restore-request",
    "HX-Request": "hx-request",
    "HX-Target": "hx-target",
    "HX-Trigger": "hx-trigger",
    "HX-Trigger-Name": "hx-trigger-name",
}
