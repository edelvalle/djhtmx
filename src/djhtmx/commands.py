"""djhtmx commands.

A command is a unit of work in the dispatch pipeline.  Three named
unions describe their roles:

- `Command` — *handler-yieldable*.  These are the public API surface
  for `_handle_event`, `_handle_sse_events`, and HTTP event handlers.
- `InternalCommand` — *queue-only*.  Handlers must not yield these;
  they are created by the transport layer or by the processor itself.
- `ProcessedCommand` — *transport input*.  What the
  `CommandBatch`/HTTP serializer/SSE serializer consumes.  Mostly
  overlaps with the wire-effect subset of `Command`, plus `SendHtml`
  (the processor's carrier for rendered HTML).

`SendHtml` is intentionally absent from `Command` and `InternalCommand`:
handlers don't yield it and it never appears in `CommandQueue`.  The
`Render` case in `CommandProcessor._run_command` synthesises it inline
and yields it out to the transport.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import TYPE_CHECKING, Any, Literal

from django.db import models
from django.http import QueryDict
from django.shortcuts import resolve_url
from django.utils.safestring import SafeString

from .component import HtmxComponent

if TYPE_CHECKING:
    from .sse import SSEEventEnvelope

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SendHtml:
    """*Internal / transport-output.*

    Carries rendered component HTML from the processor to the transport.
    Produced exclusively by `CommandProcessor._run_command`'s `Render`
    case.  Never yielded by application code, never enqueued into
    `CommandQueue`.  The transport (HTTP body / SSE OOB fragment)
    appends `content` to its output.
    """

    content: SafeString
    debug_trace: str | None = None


@dataclass(slots=True)
class PushURL:
    """Push a new entry onto the browser history without navigating.

    Yield from a handler when the URL should reflect a state change
    that's recoverable via the browser back button (filter applied,
    page changed, modal opened-with-URL).  Sets the `HX-Push-Url`
    HTTP response header; over SSE, carried via the browser command
    sink.
    """

    url: str
    command: Literal["push_url"] = "push_url"

    @classmethod
    def from_params(cls, params: QueryDict):
        """Build from the current query parameters."""
        return cls("?" + params.urlencode())

    @classmethod
    def to(cls, to: Callable[..., Any] | models.Model | str, *args, **kwargs):
        """Build from a Django URL target (view function, model, or URL name)."""
        return cls(resolve_url(to, *args, **kwargs))


@dataclass(slots=True)
class ReplaceURL:
    """Replace the current browser history entry without navigating.

    Like `PushURL` but does not create a new history entry, so the
    back button still returns to the previous page.  Use for transient
    state changes that shouldn't litter history (e.g. URL-synced
    filters that change on every keystroke).
    """

    url: str
    command: Literal["replace_url"] = "replace_url"

    @classmethod
    def from_params(cls, params: QueryDict):
        """Build from the current query parameters."""
        return cls("?" + params.urlencode())

    @classmethod
    def to(cls, to: Callable[..., Any] | models.Model | str, *args, **kwargs):
        """Build from a Django URL target (view function, model, or URL name)."""
        return cls(resolve_url(to, *args, **kwargs))


@dataclass(slots=True)
class Destroy:
    """Remove a component from the page and from djhtmx state.

    Yield from a handler to delete a component.  Cascades to the
    component's children (registered via `BuildAndRender`'s
    `parent_id`).  On the wire becomes an OOB delete fragment
    (`<div id="..." hx-swap-oob="delete"></div>`); on the server side
    the session state for the component (and its children) is
    unregistered.

    Field `component_id` is the djhtmx component id, not a DOM id.
    """

    component_id: str
    command: Literal["destroy"] = "destroy"


@dataclass(slots=True)
class Redirect:
    """Navigate the browser to a new URL.

    Yield from a handler to trigger a full-page navigation.  HTMX
    processes this via the `HX-Redirect` response header (HTTP) or via
    the browser command sink (SSE).  Suppresses any `PushURL`/`ReplaceURL`
    in the same response — a full nav makes them meaningless.
    """

    url: str
    command: Literal["redirect"] = "redirect"

    @classmethod
    def to(cls, to: Callable[[], Any] | models.Model | str, *args, **kwargs):
        """Build from a Django URL target (view function, model, or URL name)."""
        return cls(resolve_url(to, *args, **kwargs))


@dataclass(slots=True)
class Open:
    """Open a URL in a new browser window or tab.

    Yield from a handler when the user action should produce a new
    window (download link, external page).  `target` follows the
    HTML `target` attribute conventions (`_blank`, `_self`, ...).
    `rel` defaults to `noopener noreferrer` for `_blank` safety.
    """

    url: str
    name: str = ""
    rel: str = "noopener noreferrer"
    target: str = "_blank"

    command: Literal["open-tab"] = "open-tab"

    @classmethod
    def to(cls, to: Callable[[], Any] | models.Model | str, *args, **kwargs):
        """Build from a Django URL target (view function, model, or URL name)."""
        return cls(resolve_url(to, *args, **kwargs))


@dataclass(slots=True)
class Focus:
    """Move browser focus to the DOM element matching `selector`.

    Yield from a handler after rendering an input the user should type
    into next (validation error, modal opened, form mode toggled).
    Resolves via `document.querySelector(selector).focus()` after HTMX
    settles the swap.
    """

    selector: str
    command: Literal["focus"] = "focus"


@dataclass(slots=True)
class ScrollIntoView:
    """Scroll the DOM element matching `selector` into the viewport.

    Yield from a handler when an action moves the user's attention to
    a specific element (jump-to-error, anchor scroll, focused item in
    a long list).

    `behavior`/`block` map to the native
    `Element.scrollIntoView({behavior, block})` options.
    `if_not_visible` skips the scroll when the element is already
    fully in view, avoiding unnecessary jolts.
    """

    selector: str
    behavior: Literal["auto", "smooth", "instant"] = "smooth"
    block: Literal["start", "center", "end", "nearest"] = "center"
    if_not_visible: bool = False
    command: Literal["scroll_into_view"] = "scroll_into_view"


@dataclass(slots=True)
class Execute:
    """Invoke an event handler by name on a component, server-side.

    Yield from a handler to chain into another component's handler
    without duplicating its logic — equivalent to "as if HTMX had
    POSTed to that handler".  Synchronous in the current dispatch
    cycle, can cascade through `Emit`/`Signal`/more `Execute`.

    The HTTP endpoint also creates an `Execute` to bootstrap the
    dispatch from an incoming request; the same case in
    `CommandProcessor._run_command` serves both uses.

    Contrast with `DispatchDOMEvent` (browser-side event fire) — both
    "trigger something" but in different planes: `Execute` runs a
    Python handler, `DispatchDOMEvent` fires a DOM CustomEvent.
    """

    component_id: str
    event_handler: str
    event_data: dict[str, Any]


@dataclass(slots=True)
class DispatchDOMEvent:
    """Fire a DOM `CustomEvent` in the browser.

    Yield from a handler to signal the browser-side environment about
    a server-side state change.  Anything listening via
    `addEventListener(event, ...)` on `target` (or matching, if
    `bubbles=True`) will receive the `CustomEvent` with
    `event.detail = detail`.

    Useful for bridging djhtmx with non-djhtmx UI (charts, third-party
    JS libraries, custom Web Components).  Contrast with `Execute`
    (server-side handler invocation).

    Fires after HTMX settles the swap (`HX-Trigger-After-Settle`).
    """

    target: str
    event: str
    detail: Any
    bubbles: bool = False
    cancelable: bool = False
    composed: bool = False
    command: Literal["dispatch_dom_event"] = "dispatch_dom_event"


@dataclass(slots=True)
class SkipRender:
    """Suppress the default render of a component for this handler call.

    Yield from a handler when the handler's effect is captured by
    other yielded commands (e.g. an explicit `Render` of a different
    component, a `Redirect`, or a `Destroy`) and re-rendering this
    component would be wasteful or wrong.

    Only suppresses the *implicit* default render that
    `_process_emitted_commands` adds for the running component; an
    explicit `Render(self)` elsewhere in the yields still takes
    effect.
    """

    component: HtmxComponent


@dataclass(slots=True)
class BuildAndRender:
    """Build a new component instance and render it.

    Yield from a handler when the response should include a component
    that wasn't already in the session (e.g. a parent dynamically
    creating a child).  `state` is the constructor kwargs;
    `parent_id` registers the new component as a child of the given
    component so the cascade on `Destroy(parent_id)` reaches it.

    `oob` controls HTMX out-of-band placement (defaults to `"true"` —
    swap by id; other values like `"beforeend: #target"` insert at the
    targeted position).

    The convenience constructors (`append`, `prepend`, `after`,
    `before`, `update`) cover the common OOB patterns.
    """

    component: type[HtmxComponent]
    state: dict[str, Any]
    oob: str = "true"
    parent_id: str | None = None
    timestamp: int = dataclass_field(default_factory=time.monotonic_ns)

    @classmethod
    def append(
        cls,
        target_: str,
        component_: type[HtmxComponent],
        parent_id: str | None = None,
        **state,
    ):
        """Build + render with `oob="beforeend: target_"` (insert at end of target)."""
        return cls(
            component=component_,
            state=state,
            oob=f"beforeend: {target_}",
            parent_id=parent_id,
        )

    @classmethod
    def prepend(
        cls,
        target_: str,
        component_: type[HtmxComponent],
        parent_id: str | None = None,
        **state,
    ):
        """Build + render with `oob="afterbegin: target_"` (insert at start of target)."""
        return cls(
            component=component_,
            state=state,
            oob=f"afterbegin: {target_}",
            parent_id=parent_id,
        )

    @classmethod
    def after(
        cls,
        target_: str,
        component_: type[HtmxComponent],
        parent_id: str | None = None,
        **state,
    ):
        """Build + render with `oob="afterend: target_"` (insert just after target)."""
        return cls(
            component=component_,
            state=state,
            oob=f"afterend: {target_}",
            parent_id=parent_id,
        )

    @classmethod
    def before(
        cls,
        target_: str,
        component_: type[HtmxComponent],
        parent_id: str | None = None,
        **state,
    ):
        """Build + render with `oob="beforebegin: target_"` (insert just before target)."""
        return cls(
            component=component_,
            state=state,
            oob=f"beforebegin: {target_}",
            parent_id=parent_id,
        )

    @classmethod
    def update(cls, component: type[HtmxComponent], **state):
        """Build + render with the default `oob="true"` (replace by component id)."""
        return cls(component=component, state=state)


@dataclass(slots=True)
class Render:
    """Render an existing component instance.

    Yield from a handler to re-render a component (either `self` or
    another component the handler reached for).  The processor turns
    `Render` into a `SendHtml` carrying the rendered HTML; the
    transport places it as an OOB fragment keyed by component id.

    `template` overrides the component's default template (used by
    partial renders).  `oob` controls HTMX OOB placement (default
    `"true"`).  `lazy=True` defers the actual render to a follow-up
    request triggered when the placeholder enters the DOM (used for
    expensive components).  `context` overrides the template render
    context wholesale.
    """

    component: HtmxComponent
    template: str | None = None
    oob: str = "true"
    lazy: bool | None = None
    context: dict[str, Any] | None = None
    timestamp: int = dataclass_field(default_factory=time.monotonic_ns)


@dataclass(slots=True)
class Emit:
    """Fan out a Python event to in-session `_handle_event` listeners.

    Yield from a handler to wake any component in the *same djhtmx
    session* that subscribes to `type(event)` via `_handle_event`.
    Synchronous and session-local: handlers run inside the same
    dispatch cycle, no Redis publish.

    `Emit` is **always** session-local.  For cross-session delivery
    use `djhtmx.sse.emit_sse_event`, which publishes to Redis and
    wakes consumers on other workers / other browser sessions.
    """

    event: Any
    timestamp: int = dataclass_field(default_factory=time.monotonic_ns)


@dataclass(slots=True)
class Signal:
    """*Internal.*  Wake components subscribed to one or more signals.

    The processor enqueues this when a component's query-patcher
    parameters change, to fire the subscriber list of each signal.
    Application code does not yield `Signal` directly; declare
    subscriptions on the component and trust the framework to fire
    them.

    `names` is a set of `(signal_name, emitter_component_id)` tuples.
    """

    names: set[tuple[str, str]]
    timestamp: int = dataclass_field(default_factory=time.monotonic_ns)


@dataclass(slots=True)
class HandleSSEEvents:
    """*Internal.*  Deliver a batch of SSE envelopes to a component.

    The SSE drain enqueues one `HandleSSEEvents` per consumer that has
    pending events.  `CommandProcessor._run_command`'s case loads the
    component, calls `_handle_sse_events(envelope)` for each envelope,
    wraps handler exceptions as `Emit(HtmxUnhandledError(...))`, and
    routes the yielded commands through `_process_emitted_commands`
    with `during_execute=False`.

    Application code does not yield this; it's the SSE entry point
    into the unified command pipeline.
    """

    component_id: str
    envelopes: tuple[SSEEventEnvelope[Any], ...]


# ---------------------------------------------------------------------------
# Unions.
#
# `Command`         — handler-yieldable.  Public API surface.
# `InternalCommand` — queue-only.  Handlers must not yield.
# `ProcessedCommand`— transport input.  CommandBatch consumes these.
# ---------------------------------------------------------------------------

Command = (
    Render
    | BuildAndRender
    | Destroy
    | Emit
    | SkipRender
    | Open
    | Focus
    | ScrollIntoView
    | Redirect
    | DispatchDOMEvent
    | PushURL
    | ReplaceURL
    | Execute
)

InternalCommand = Signal | HandleSSEEvents

ProcessedCommand = (
    SendHtml
    | Destroy
    | Open
    | Focus
    | ScrollIntoView
    | Redirect
    | DispatchDOMEvent
    | PushURL
    | ReplaceURL
)


__all__ = (
    "BuildAndRender",
    "Command",
    "Destroy",
    "DispatchDOMEvent",
    "Emit",
    "Execute",
    "Focus",
    "HandleSSEEvents",
    "Open",
    "ProcessedCommand",
    "PushURL",
    "Redirect",
    "Render",
    "ReplaceURL",
    "ScrollIntoView",
    "SendHtml",
    "Signal",
    "SkipRender",
)
