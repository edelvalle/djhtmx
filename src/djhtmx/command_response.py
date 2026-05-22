"""Transport-neutral command accumulator and transport serializers.

`CommandBatch` collects a stream of `ProcessedCommand` values into an
intermediate shape (HTML fragments + browser commands + URL state) that
doesn't know about HTTP or SSE.  Transport-specific serializers turn the
batch into wire output:

- `to_http_response` — HTTP endpoint response, with HTMX headers and
  `HX-Trigger-After-Settle` payloads for browser commands.
- `to_sse_fragments` — list of OOB HTML fragments for an SSE drain.
  `Open` is supported via the session-scoped command sink; other
  browser commands and URL mutations have no carrier until Phase 2.6
  generalizes the sink, and are logged as unsupported.

This module is the single place that decides how a `ProcessedCommand`
maps to wire effects.  `urls.endpoint` and `sse.py` consume it without
duplicating that match.

See `docs/plans/sse-generalized-worker.md` for the rationale.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import assert_never

from django.http.response import HttpResponse
from django.utils.html import format_html
from django.utils.safestring import SafeString

from .commands import (
    Destroy,
    DispatchDOMEvent,
    Focus,
    Open,
    ProcessedCommand,
    PushURL,
    Redirect,
    ReplaceURL,
    ScrollIntoView,
    SendHtml,
)
from .component import Triggers
from .utils import compact_hash

logger = logging.getLogger(__name__)

BrowserCommand = Focus | ScrollIntoView | Open | DispatchDOMEvent


@dataclass(slots=True)
class CommandBatch:
    """Transport-neutral accumulator for processed commands.

    HTML-producing commands (`SendHtml`, `Destroy`) land in `html` as
    pre-rendered OOB fragments.  Browser-effect commands collect in
    `browser_commands`.  URL-mutating commands store the last value seen
    in `redirect`/`push_url`/`replace_url` (last writer wins, matching
    the previous per-iteration overwrite behavior of `urls.endpoint`).
    """

    html: list[str | SafeString] = dataclass_field(default_factory=list)
    browser_commands: list[BrowserCommand] = dataclass_field(default_factory=list)
    redirect: Redirect | None = None
    push_url: PushURL | None = None
    replace_url: ReplaceURL | None = None

    def add(self, command: ProcessedCommand) -> None:
        match command:
            case SendHtml(content):
                self.html.append(content)
            case Destroy(component_id):
                self.html.append(
                    format_html(
                        '<div id="{component_id}" hx-swap-oob="delete"></div>',
                        component_id=component_id,
                    )
                )
            case Redirect():
                self.redirect = command
            case PushURL():
                self.push_url = command
            case ReplaceURL():
                self.replace_url = command
            case Focus() | ScrollIntoView() | Open() | DispatchDOMEvent():
                self.browser_commands.append(command)
            case unreachable:
                assert_never(unreachable)

    @classmethod
    def from_processed(cls, commands: Iterable[ProcessedCommand]) -> CommandBatch:
        batch = cls()
        for command in commands:
            batch.add(command)
        return batch


def to_http_response(batch: CommandBatch) -> HttpResponse:
    """Serialize a `CommandBatch` to an HTTP response with HTMX headers."""
    headers: dict[str, str] = {}
    triggers = Triggers()

    for command in batch.browser_commands:
        match command:
            case Focus(selector):
                triggers.after_settle("hxFocus", selector)
            case ScrollIntoView(selector, behavior, block, if_not_visible):
                triggers.after_settle(
                    "hxScrollIntoView",
                    {
                        "selector": selector,
                        "behavior": behavior,
                        "block": block,
                        "if_not_visible": if_not_visible,
                    },
                )
            case Open(url, name, target, rel):
                triggers.after_settle(
                    "hxOpenURL",
                    {"url": url, "name": name, "target": target, "rel": rel},
                )
            case DispatchDOMEvent(target, event, detail, bubbles, cancelable, composed):
                triggers.after_settle(
                    "hxDispatchDOMEvent",
                    {
                        "event": event,
                        "target": target,
                        "detail": detail,
                        "bubbles": bubbles,
                        "cancelable": cancelable,
                        "composed": composed,
                    },
                )
            case unreachable:
                assert_never(unreachable)

    if batch.redirect is not None:
        # HX-Redirect triggers a full navigation, so URL manipulation headers
        # are meaningless and can cause HTMX to skip the redirect.
        headers["HX-Redirect"] = batch.redirect.url
    else:
        if batch.push_url is not None:
            headers["HX-Push-Url"] = batch.push_url.url
        if batch.replace_url is not None:
            headers["HX-Replace-Url"] = batch.replace_url.url

    body = "\n\n".join(str(fragment) for fragment in batch.html)
    return HttpResponse(body, headers=headers | triggers.headers)


def to_sse_fragments(batch: CommandBatch, session_id: str) -> list[str]:
    """Serialize a `CommandBatch` as a list of SSE OOB HTML fragments.

    SSE payloads cannot use HTMX response headers, so URL-mutating
    commands and most browser commands have no carrier in the MVP shape
    of the SSE wire protocol.  Only `Open` is supported via the
    session-scoped command sink; other browser commands (`Focus`,
    `ScrollIntoView`, `DispatchDOMEvent`) and URL commands (`Redirect`,
    `PushURL`, `ReplaceURL`) are logged as unsupported.

    Phase 2.6 of the SSE-generalized-worker plan generalizes the
    command sink to carry the full browser command set; at that point
    this function will route them all through the sink.
    """
    fragments = [str(fragment) for fragment in batch.html]

    for command in batch.browser_commands:
        match command:
            case Open() as open_command:
                fragments.append(_render_open_command(session_id, open_command))
            case Focus() | ScrollIntoView() | DispatchDOMEvent():
                logger.error(
                    "%s is not supported from SSE handlers yet "
                    "(needs Phase 2.6 generalized command sink): %s",
                    type(command).__name__,
                    command,
                )
            case _ as unreachable:
                assert_never(unreachable)

    if batch.redirect is not None:
        logger.error(
            "Redirect is not supported from SSE handlers yet "
            "(needs Phase 2.6 generalized command sink): %s",
            batch.redirect,
        )
    if batch.push_url is not None:
        logger.error(
            "PushURL is not supported from SSE handlers yet "
            "(needs Phase 2.6 generalized command sink): %s",
            batch.push_url,
        )
    if batch.replace_url is not None:
        logger.error(
            "ReplaceURL is not supported from SSE handlers yet "
            "(needs Phase 2.6 generalized command sink): %s",
            batch.replace_url,
        )

    return fragments


def _render_open_command(session_id: str, command: Open) -> str:
    session_hash = compact_hash(session_id)
    sink_id = f"djhtmx-sse-commands-{session_hash}"
    return str(
        format_html(
            """
            <div hx-swap-oob="beforeend: #{sink_id}">
              <div data-command="open"
                   data-session="{session_hash}"
                   data-url="{url}"
                   data-name="{name}"
                   data-target="{target}"
                   data-rel="{rel}"></div>
            </div>
            """,
            sink_id=sink_id,
            session_hash=session_hash,
            url=command.url,
            name=command.name,
            target=command.target,
            rel=command.rel,
        ).strip()
    )


__all__ = ("BrowserCommand", "CommandBatch", "to_http_response", "to_sse_fragments")
