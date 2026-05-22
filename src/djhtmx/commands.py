from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any, Literal

from django.db import models
from django.http import QueryDict
from django.shortcuts import resolve_url
from django.utils.safestring import SafeString

from .component import HtmxComponent

__all__ = (
    "BuildAndRender",
    "Command",
    "Destroy",
    "DispatchDOMEvent",
    "Emit",
    "Execute",
    "Focus",
    "Open",
    "PushURL",
    "Redirect",
    "Render",
    "ReplaceURL",
    "ScrollIntoView",
    "SendHtml",
    "Signal",
    "SkipRender",
)


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SendHtml:
    content: SafeString

    # debug trace for troubleshooting
    debug_trace: str | None = None


@dataclass(slots=True)
class PushURL:
    url: str
    command: Literal["push_url"] = "push_url"

    @classmethod
    def from_params(cls, params: QueryDict):
        return cls("?" + params.urlencode())

    @classmethod
    def to(cls, to: Callable[..., Any] | models.Model | str, *args, **kwargs):
        return cls(resolve_url(to, *args, **kwargs))


@dataclass(slots=True)
class ReplaceURL:
    url: str
    command: Literal["replace_url"] = "replace_url"

    @classmethod
    def from_params(cls, params: QueryDict):
        return cls("?" + params.urlencode())

    @classmethod
    def to(cls, to: Callable[..., Any] | models.Model | str, *args, **kwargs):
        return cls(resolve_url(to, *args, **kwargs))


@dataclass(slots=True)
class Destroy:
    "Destroys the given component in the browser and in the caches."

    component_id: str
    command: Literal["destroy"] = "destroy"


@dataclass(slots=True)
class Redirect:
    "Executes a browser redirection to the given URL."

    url: str
    command: Literal["redirect"] = "redirect"

    @classmethod
    def to(cls, to: Callable[[], Any] | models.Model | str, *args, **kwargs):
        return cls(resolve_url(to, *args, **kwargs))


@dataclass(slots=True)
class Open:
    "Open a new window with the URL."

    url: str
    name: str = ""
    rel: str = "noopener noreferrer"
    target: str = "_blank"

    command: Literal["open-tab"] = "open-tab"

    @classmethod
    def to(cls, to: Callable[[], Any] | models.Model | str, *args, **kwargs):
        return cls(resolve_url(to, *args, **kwargs))


@dataclass(slots=True)
class Focus:
    "Executes a '.focus()' on the browser element that matches `selector`"

    selector: str
    command: Literal["focus"] = "focus"


@dataclass(slots=True)
class ScrollIntoView:
    "Scrolls the browser element that matches `selector` into view"

    selector: str
    behavior: Literal["auto", "smooth", "instant"] = "smooth"
    block: Literal["start", "center", "end", "nearest"] = "center"
    if_not_visible: bool = False
    command: Literal["scroll_into_view"] = "scroll_into_view"


@dataclass(slots=True)
class Execute:
    component_id: str
    event_handler: str
    event_data: dict[str, Any]


@dataclass(slots=True)
class DispatchDOMEvent:
    "Dispatches a DOM CustomEvent in the given target."

    target: str
    event: str
    detail: Any
    bubbles: bool = False
    cancelable: bool = False
    composed: bool = False
    command: Literal["dispatch_dom_event"] = "dispatch_dom_event"


@dataclass(slots=True)
class SkipRender:
    "Instruct the HTMX engine to avoid the render of the component."

    component: HtmxComponent


@dataclass(slots=True)
class BuildAndRender:
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
        return cls(
            component=component_,
            state=state,
            oob=f"beforebegin: {target_}",
            parent_id=parent_id,
        )

    @classmethod
    def update(cls, component: type[HtmxComponent], **state):
        return cls(component=component, state=state)


@dataclass(slots=True)
class Render:
    component: HtmxComponent
    template: str | None = None
    oob: str = "true"
    lazy: bool | None = None
    context: dict[str, Any] | None = None
    timestamp: int = dataclass_field(default_factory=time.monotonic_ns)


@dataclass(slots=True)
class Emit:
    "Emit a backend-only event."

    event: Any
    timestamp: int = dataclass_field(default_factory=time.monotonic_ns)


@dataclass(slots=True)
class Signal:
    "Emit a backend-only signal."

    names: set[tuple[str, str]]  # set[tuple[signal name, emitter component id]]
    timestamp: int = dataclass_field(default_factory=time.monotonic_ns)


Command = (
    Destroy
    | Redirect
    | Focus
    | ScrollIntoView
    | DispatchDOMEvent
    | SkipRender
    | BuildAndRender
    | Render
    | Emit
    | Signal
    | Execute
    | Open
    | PushURL
    | ReplaceURL
)
