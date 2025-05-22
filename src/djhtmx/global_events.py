from dataclasses import dataclass


@dataclass
class HtmxUnhandledError:
    """HTMX triggers this event for any HTMX handler that fails unhandled.

    Applications could subscribe to this event to have last-resource general error recovery
    mechanism.

    """

    error: BaseException | None
    handler_annotations: dict | None = None
