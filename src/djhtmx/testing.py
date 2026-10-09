from collections import UserList, defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from copy import deepcopy
from functools import reduce
from typing import Any, Literal, assert_never, get_args, overload
from urllib.parse import urlparse
from warnings import deprecated

from asgiref.sync import async_to_sync
from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.auth.models import AnonymousUser
from django.test import Client
from lxml import html
from pygments import highlight
from pygments.formatters import TerminalTrueColorFormatter  # type: ignore[attr-defined]
from pygments.lexers import HtmlLexer  # type: ignore[attr-defined]

from . import json
from .command_processor import CommandProcessor, CommandRecorder, RecordedCommand
from .commands import (
    Command,
    Destroy,
    DispatchDOMEvent,
    Emit,
    Focus,
    Open,
    PushURL,
    Redirect,
    ReplaceURL,
    ScrollIntoView,
    SendHtml,
)
from .component import HtmxComponent
from .introspection import parse_request_data
from .repo import Repository, Session, signer
from .utils import get_fqn, get_params

__all__ = ("CapturedCommands", "CapturedEvents", "Htmx")


class Htmx:
    def __init__(self, client: Client):
        self._client = client

    @property
    def url(self) -> str:
        return f"{self.path}?{self.query_string}".rstrip("?")

    def navigate_to(
        self,
        path: str,
        data: Mapping[str, object] | None = None,
        follow: bool = True,
        secure: bool = False,
        *,
        headers: Mapping[str, str] | None = None,
        query_params: Mapping[str, object] | None = None,
        **extra: str,
    ):
        """Load the page at `path`, as a browser would, and make it the page under test.

        The arguments are those of Django's `Client.get`:meth:, except that `follow` defaults to
        True, so a redirect lands on the page it points to.  The page must answer with a 2xx
        status and carry a djhtmx session, or this fails with an `AssertionError`.

        Afterwards `dom` holds the rendered HTML, `path` and `query_string` the page's URL, and the
        components it placed can be looked up.  Whatever a previous page held is gone.

        """
        response = self._client.get(
            path,
            data,
            follow,
            secure,
            headers=headers,
            # The stubs predate `query_params`, added in Django 5.1.
            query_params=query_params,  # type: ignore[arg-type]
            **extra,
        )
        assert 200 <= response.status_code < 300
        self.path = response.request["PATH_INFO"]
        self.query_string = response.request["QUERY_STRING"]

        self.dom = html.fromstring(response.content)
        session_id = reduce(
            lambda session, element: (
                session or json.loads(element.attrib["hx-headers"]).get("HX-Session")
            ),
            self.dom.cssselect("[hx-headers]"),
            None,
        )
        assert session_id, "Can't find djhtmx session id"
        session_id = signer.unsign(session_id)

        self._user = response.context.get("user") or AnonymousUser()
        self._repo = self._build_repository(session_id)

    def get_component_by_type[C: HtmxComponent](self, component_type: type[C]) -> C:
        [component] = self._repo.get_components_by_names(component_type.__name__)
        assert isinstance(component, component_type)
        return component

    def get_components_by_type[C: HtmxComponent](self, component_type: type[C]) -> Iterable[C]:
        return self._repo.get_components_by_names(component_type.__name__)  # type: ignore

    def get_component_by_id(self, component_id: str):
        component = self._repo.get_component_by_id(component_id)
        assert isinstance(component, HtmxComponent)
        return component

    def type_into(self, selector: str | html.HtmlElement, text: str, clear=False):
        """Set the value of an input or textarea, by "typing" into it.

        Appends `text` to what the element already holds, or replaces it when `clear` is true.

        """
        element = self._select(selector)
        if (
            element.tag == "input" and element.attrib.get("type", "text") == "text"
        ) or element.tag == "textarea":
            if clear:
                element.attrib["value"] = text
            else:
                element.attrib["value"] = element.attrib.get("value", "") + text
        else:
            assert False, f"Can't type in element {element}"

    def find_by_text(self, text: str) -> html.HtmlElement:
        return self.dom.xpath(f"//*[text()='{text}']")

    def select(self, selector: str) -> list[html.HtmlElement]:
        return self.dom.cssselect(selector)

    def print(self, element: html.HtmlElement):
        print(
            highlight(
                html.tostring(element, pretty_print=True, encoding="unicode"),
                HtmlLexer(),
                TerminalTrueColorFormatter(),
            )
        )

    def _select(self, selector: str | html.HtmlElement) -> html.HtmlElement:
        if isinstance(selector, str):
            [element] = self.dom.cssselect(selector)
        else:
            element = selector
        return element

    def trigger(self, selector: str | html.HtmlElement):
        """Fire the event bound to the element, and apply everything it produces.

        Delivers the session's pending SSE events before returning, as `dispatch_event`:meth:
        does.

        """
        element = self._select(selector)

        # mutate in case of a checkbox and radios
        match element.tag, element.attrib.get("type"):
            case "input", "checkbox":
                if "checked" in element.attrib:
                    element.attrib.pop("checked")
                else:
                    element.attrib["checked"] = ""
            case "input", "radio":
                if name := element.attrib.get("name"):
                    for radio in self.dom.cssselect(f'input[type=radio][name="{name}"]'):
                        radio.attrib.pop("checked", None)
                element.attrib["checked"] = ""
            case _:
                pass

        [_, component_id, event_handler] = element.attrib["hx-post"].rsplit("/", 2)

        # gather values
        vals = defaultdict(list)
        if include := element.attrib.get("hx-include"):
            for element in self.dom.cssselect(include):
                name = element.attrib["name"]
                value = element.attrib.get("value", "")
                match element.tag, element.attrib.get("type"):
                    case _, "checkbox":
                        if "checked" in element.attrib:
                            vals[name].append(value or "on")
                    case _, "radio":
                        if "checked" in element.attrib:
                            vals[name].append(value)
                    case "select", _:
                        for option in element.cssselect("option[selected]"):
                            vals[name].append(option.attrib.get("value", ""))
                    case _, _:
                        vals[name].append(value)

        vals |= json.loads(element.attrib.get("hx-vals", "{}"))
        self.dispatch_event(component_id, event_handler, parse_request_data(vals))

    def send[**P](self, method: Callable[P, Any], *args: P.args, **kwargs: P.kwargs):
        """Run a component's event handler, and apply everything it produces.

        Delivers the session's pending SSE events before returning, as `dispatch_event`:meth:
        does.

        """
        assert not args, "All parameters have to be passed by name"
        self.dispatch_event(method.__self__.id, method.__name__, kwargs)  # type: ignore

    def dispatch_event(self, component_id: str, event_handler: str, kwargs: dict[str, Any]):
        """Run the named handler of the component, and apply everything it produces.

        Always ends by delivering the session's pending SSE events, the way the browser receives
        them between requests, so a component's reaction to one is applied without asking; see
        `drain_sse_events`:meth:.

        """
        # One repository per send, as each request in production gets its own: the render-cycle
        # caches must not outlive the send.
        self._repo = self._build_repository(self._repo.session.id)
        with Repository.activate(self._repo):
            commands = list(self._repo.dispatch_event(component_id, event_handler, kwargs))
        navigate_to_url = None
        for command in commands:
            match command:
                case SendHtml(content):
                    self._apply_oob_html(str(content))

                case Destroy(component_id):
                    target = self.dom.get_element_by_id(component_id)
                    parent = target.getparent()
                    if parent is not None:
                        parent.remove(target)

                case Redirect(url) | Open(url):
                    navigate_to_url = url

                case PushURL(url) | ReplaceURL(url):
                    parsed_url = urlparse(url)
                    # As the browser does, a bare `?query` keeps the current path.  Not `urljoin`:
                    # it resolves an empty `?` to the current query instead of clearing it.
                    self.path = parsed_url.path or self.path
                    self.query_string = parsed_url.query

                case Focus() | ScrollIntoView() | DispatchDOMEvent():
                    pass

        self.drain_sse_events()

        if navigate_to_url:
            self.navigate_to(navigate_to_url)

    @contextmanager
    def assertEmits[E](self, event_class: type[E]) -> "Iterator[CapturedEvents[E]]":
        """Assert that at least one `event_class` event is emitted inside the block.

        The block receives a `CapturedEvents`:class: standing for the events of that class.  If no
        event was found it will raise at exit.

        Example::

            with self.htmx.assertEmits(FeedbackMessage) as captured:
                self.htmx.send(editor.rebuild_the_items)
            event = captured.get_event()
            self.assertIn("Open an item first", event.body)

        """
        with self.assertYields(Emit) as emits:
            captured = CapturedEvents(emits, event_class)
            yield captured
        # Outside the block above, so that everything it captures on its way out counts.
        captured.get_event()

    @overload
    def assertYields[C: Command](
        self, command_class: type[C]
    ) -> "AbstractContextManager[CapturedCommands[C]]": ...

    @overload
    def assertYields(self, command_class: None) -> AbstractContextManager[None]: ...

    @contextmanager
    def assertYields[C: Command](self, command_class: type[C] | None) -> Iterator[Any]:
        """Assert that at least one `command_class` command is yielded inside the block.

        Every command of that class yielded inside the block is captured::

            with self.htmx.assertYields(Redirect) as commands:
                self.htmx.send(editor.save_and_leave)
            [redirect] = commands  # Ensure only 1 Redirect
            self.assertEqual(redirect.url, self.owner_url)

        .. note:: The sequence is a live view, not a snapshot: the block receives it before its
           first command exists.  This means that it might mutate inside the block.

           We suggest to make validations outside of the block, after this method has asserted that
           `command_class` was produced.

        .. rubric:: Validating no yields

        Pass `None` to assert no command is ever yielded by the handler::

            with self.htmx.assertYields(None):
                self.htmx.send(editor.open_the_item, item=self.item)

        Only what a handler yields is watched, so the default `Render` djhtmx adds for a handler
        that yielded nothing of its own -- djhtmx's command, not the handler's -- is not what
        makes `assertYields(None)` fail.

        """
        if command_class is None:
            with self.capturing() as captured:
                yield None
            assert not captured.did_yield, (
                f"Expected nothing to be yielded inside the block, but got: {captured.describe()}"
            )
        else:
            with self.capturing(command_class) as captured:
                yield captured
            # A command lives in `djhtmx.commands`, so its module tells a reader nothing; an
            # event's does, since two applications can name an event alike.
            command = get_fqn(command_class).removeprefix("djhtmx.commands.")
            assert captured, (
                f"No {command} was yielded inside the block; got: {captured.describe()}"
            )

    @contextmanager
    def capturing(self, *command_classes: type[Command]) -> "Iterator[CapturedCommands[Command]]":
        """Capture the commands the handlers yield inside the block.

        This is the mechanism the assertions above are built on, for a test that wants to look at
        what a dispatch produced instead of stating up front what it must produce::

            with self.htmx.capturing(SkipRender, Emit) as captured:
                self.htmx.send(editor.save)
            [skip_render, emit] = captured

        Called with no class it captures every kind of command.  The capture holds the commands of
        the given classes in the order the handlers yielded them, and stays empty both for a block
        whose handlers yielded nothing and for one that yielded only commands of other classes;
        `CapturedCommands.did_yield`:attr: tells those apart.

        The order of the capture commands is the same order in which they are yielded inside the
        block.  A note of caution though.  HTMX does not process commands as soon as they are
        yielded by components.  If several components react to the actions, you'll get all commands
        from all components in the order as they are yielded, not processed.  Processing some
        commands (e.g Emit) might produce event more commands and the process queue order is not the
        same as the yield order.  Relying on this ordering might be tricky and can change based on
        the process queue ordering.

        """
        with CommandProcessor._install_recorder() as recorder:
            yield CapturedCommands(recorder, command_classes or _ALL_COMMAND_CLASSES)

    def drain_sse_events(self):
        """Deliver the session's pending SSE events and apply whatever they render.

        `dispatch_event`:meth:, and so `send`:meth: and `trigger`:meth:, always ends by doing
        this, so a test needs it only for the events raised by something other than a handler.

        """
        if sse_html := async_to_sync(self._render_sse_events)():
            self._apply_oob_html(sse_html)

    def _build_repository(self, session_id: str) -> Repository:
        return Repository(user=self._user, session=Session(session_id), params=get_params(self.url))

    async def _render_sse_events(self):
        from .sse import render_sse_events

        return await render_sse_events(self._repo.session.id, self._user)

    def _apply_oob_html(self, content: str):
        """Apply the out-of-band swaps in `content` to `dom`, as htmx 2.0.4 does in the browser.

        Every element carrying `hx-swap-oob` (or `data-hx-swap-oob`) is swapped, at any depth of
        `content`.  Those inside a `<template>` are swapped after all the others, unless the
        template is itself inside a swapped element; then they are left as they are.  The rest of
        `content` is discarded, as `hx-swap="none"` does.

        The value is `true` (replace the element with the same id), a strategy alone (applied to
        the element with the same id), or `<strategy>:<selector>`, applied to every element the
        selector designates (see `_select_oob_targets`:meth:).  An unknown strategy falls back to
        `innerHTML`, and a swap without target changes nothing.

        """
        fragments = [
            fragment
            for fragment in html.fragments_fromstring(content)
            if isinstance(fragment, html.HtmlElement)
        ]
        # Both passes are collected before swapping: the first one strips the attributes the
        # second one looks for.
        oob_elements = [
            element for fragment in fragments for element in fragment.xpath(_OOB_OUTSIDE_TEMPLATES)
        ]
        oob_elements_in_templates = [
            element
            for fragment in fragments
            for template in fragment.xpath(_TEMPLATES_LEFT_IN_THE_RESPONSE)
            for element in template.xpath(_OOB_IN_THIS_TEMPLATE)
        ]
        for element in oob_elements + oob_elements_in_templates:
            self._swap_oob_element(element)

    def _swap_oob_element(self, element: html.HtmlElement):
        oob = element.attrib.pop("hx-swap-oob", "") or element.attrib.pop("data-hx-swap-oob")
        element.attrib.pop("data-hx-swap-oob", None)
        strategy, selector = _parse_oob_value(oob)
        if selector is not None:
            targets = self._select_oob_targets(selector)
        elif (target_id := element.get("id")) is not None:
            targets = self.dom.xpath("//*[@id=$id]", id=target_id)
        else:
            targets = []
        for target in targets:
            # Each target gets its own copy, as in htmx, so that a selector matching several
            # elements fills all of them.
            incoming = deepcopy(element)
            incoming.tail = None
            parent = target.getparent()
            assert parent is not None, "The root of the page can't be swapped out of band"
            match strategy:
                case "outerHTML" if target.tag == "body":
                    # As in htmx, which can't put a <body> in a document fragment.
                    _replace_content(target, [incoming])
                case "outerHTML":
                    incoming.tail = target.tail
                    parent.replace(target, incoming)
                case "afterbegin":
                    _insert_content(target, 0, incoming, before_text=True)
                case "beforeend":
                    _insert_content(target, len(target), incoming, before_text=False)
                case "beforebegin":
                    _insert_content(parent, parent.index(target), incoming, before_text=False)
                case "afterend":
                    _insert_content(parent, parent.index(target) + 1, incoming, before_text=True)
                case "delete":
                    _remove_element(parent, target)
                case "none":
                    pass
                case "innerHTML":
                    _replace_content(target, [])
                    _insert_content(target, 0, incoming, before_text=False)
                case unreachable:
                    assert_never(unreachable)

    def _select_oob_targets(self, selector: str) -> list[html.HtmlElement]:
        """Answer with the elements `selector` designates, as htmx 2.0.4 resolves it for an OOB swap.

        htmx resolves it from the document, as a comma-separated list (commas inside `<X/>` don't
        count) of CSS selectors and its extended selectors:

        - `find X` designates the first `X` only, `body` the body, and `<X/>` is `X`.
        - `closest X`, `next X`, `previous X` and `host` are relative to an element, and designate
          nothing from the document.
        - `next`, `previous`, `document`, `window` and `root` raise `AssertionError`.
        - A leading `global ` is dropped; it only matters inside a shadow DOM.  As in htmx, it
          must come right after the colon of `hx-swap-oob`, without a space.

        The elements of the extended selectors come first, then those of the CSS selectors in
        document order.

        """
        parts = [
            _normalize_selector(part)
            for part in _split_selector_list(selector.removeprefix("global "))
        ]
        resolved = [self._resolve_selector_part(part) for part in parts]
        css_selector = ",".join(part for _, part in resolved if part is not None)
        css_targets = self.dom.cssselect(css_selector) if css_selector else []
        return [target for targets, _ in resolved for target in targets] + css_targets

    def _resolve_selector_part(self, part: str) -> tuple[list[html.HtmlElement], str | None]:
        """Answer with the elements an extended selector designates, or the CSS selector `part` is."""
        match part.partition(" "):
            case ("find", " ", argument):
                return self.dom.cssselect(_normalize_selector(argument))[:1], None
            case ("closest" | "next" | "previous", " ", str()) | ("host", "", ""):
                return [], None
            case ("body", "", ""):
                return [self.dom.body], None
            case (
                "next"
                | "nextElementSibling"
                | "previous"
                | "previousElementSibling"
                | "document"
                | "window"
                | "root",
                "",
                "",
            ):
                raise AssertionError(f"htmx fails to swap out of band into {part!r}")
            case (str(), str(), str()):
                return [], part

    @property
    @deprecated("Htmx.client is deprecated, use the client passed to Htmx instead")
    def client(self) -> Client:
        """Deprecated: the client this helper was built with."""
        return self._client

    @property
    @deprecated("Htmx.user is deprecated, use the user the test logged in instead")
    def user(self) -> AbstractBaseUser | AnonymousUser:
        """Deprecated: the user the page rendered with."""
        return self._user

    @property
    @deprecated(
        "Htmx.repo is deprecated, use get_component_by_type, get_components_by_type or "
        "get_component_by_id instead"
    )
    def repo(self) -> Repository:
        """Deprecated: the repository of the last navigation or send."""
        return self._repo

    # Keep this last.  A method named `type` shadows the builtin for every annotation that
    # follows it in the class body, and those annotations are evaluated when their method is
    # defined (Python 3.14 made them lazy, 3.13 did not), so a `type[X]` below this point raises
    # `TypeError: 'function' object is not subscriptable` at import time.
    @deprecated("Htmx.type is deprecated, use Htmx.type_into instead")
    def type(self, selector: str | html.HtmlElement, text: str, clear=False):
        """Deprecated alias of `type_into`:meth:."""
        self.type_into(selector, text, clear=clear)


class CapturedEvents[E](UserList[E]):
    """The `E` events emitted inside a watched block.

    This is what `Htmx.assertEmits`:meth: hands to its block.  It is a list, and a live one: the
    block receives it before anything has been emitted, and every read answers with what has been
    emitted so far, whether the read happens inside the block or after it::

        with htmx.assertEmits(FeedbackMessage) as captured:
            htmx.send(editor.rebuild_the_items)
        [event] = captured
        assert "Open an item first" in event.body

    Its items are the events the listeners received, with nothing standing in for them, so reading
    their attributes is checked like any other attribute access.  `get_event`:meth: answers with
    the first one and fails with a message naming what *was* emitted, which `captured[0]` cannot
    do.

    Mutating the list has no effect.

    """

    def __init__(self, emits: Sequence[Emit], event_class: type[E]):
        self._emits = emits
        self._event_class = event_class

    # `UserList` declares `data` as a plain list it owns, hence the ignore: here it is derived
    # instead, which is what makes the list live -- the block is handed this object before its first
    # event exists, and `UserList` reads every operation off `data`.
    @property
    def data(self) -> list[E]:  # type: ignore[override]
        return [emit.event for emit in self._emits if isinstance(emit.event, self._event_class)]

    def get_event(self) -> E:
        """Answer with the first event captured, or fail the assertion while there is none."""
        if events := self.data:
            return events[0]
        else:
            raise AssertionError(self.get_failure_message())

    def get_failure_message(self) -> str:
        """Build the message for a block that emitted no such event."""
        emitted = ", ".join(repr(emit.event) for emit in self._emits) or "nothing"
        return f"No {get_fqn(self._event_class)} was emitted inside the block; emitted: {emitted}"


class CapturedCommands[C: Command](UserList[C]):
    """The `C` commands that the handlers yielded inside a watched block.

    This is what `Htmx.capturing`:meth: and `Htmx.assertYields`:meth: hand to their block.  It is a
    list, and a live one: the block receives it before anything has been yielded, and every read
    answers with what has been yielded so far, which is why a test reads it after the block rather
    than inside it -- see the note in `Htmx.assertYields`:meth:.

    Its items are the commands the handlers yielded, in the order they yielded them, so reading
    their attributes is checked like any other attribute access.  An empty capture means either
    that the handlers yielded no command of the watched classes or that they yielded nothing at
    all; `did_yield`:attr: tells those apart.  `get_recorded`:meth: answers with everything they
    yielded, captured or not, which is what `describe`:meth: reports.

    Mutating the list has no effect.

    """

    def __init__(
        self,
        recorder: CommandRecorder,
        command_classes: tuple[type[C], ...],
    ):
        self._recorder = recorder
        self._start = len(recorder.commands)
        self._command_classes = command_classes

    # `UserList` declares `data` as a plain list it owns, hence the ignore: here it is derived
    # instead, which is what makes the list live -- the block is handed this object before its
    # first command exists, and `UserList` reads every operation off `data`.
    @property
    def data(self) -> list[C]:  # type: ignore[override]
        return [
            recorded.command
            for recorded in self.get_recorded()
            if isinstance(recorded.command, self._command_classes)
        ]

    @property
    def did_yield(self) -> bool:
        """Whether the handlers yielded anything at all, of the watched classes or not."""
        return bool(self.get_recorded())

    def get_recorded(self) -> list[RecordedCommand]:
        """Answer with everything the handlers yielded inside the block, captured or not."""
        return self._recorder.since(self._start)

    def describe(self) -> str:
        """Answer with what the handlers yielded inside the block, and which yielded each.

        A block that yielded nothing is described as `nothing`, which is what an assertion failure
        reports.

        """
        return ", ".join(recorded.describe() for recorded in self.get_recorded()) or "nothing"


# The members of the `Command` union, which is what a capture naming no class watches.
_ALL_COMMAND_CLASSES: tuple[type[Command], ...] = get_args(Command)


def _insert_content(
    parent: html.HtmlElement, index: int, source: html.HtmlElement, *, before_text: bool
):
    """Move the content of `source`, its text and children, to position `index` of `parent`.

    The text that precedes the child at `index` (the tail of the previous child, or the text of
    `parent`) stays before the moved content, unless `before_text` is true: then it follows it.

    """
    children = list(source)
    preceding = parent[index - 1] if index else None
    slot_text = (parent.text if preceding is None else preceding.tail) or ""
    if before_text:
        leading_text, trailing_text = source.text or "", slot_text
    else:
        leading_text, trailing_text = slot_text + (source.text or ""), ""
    for offset, child in enumerate(children):
        parent.insert(index + offset, child)
    if children:
        children[-1].tail = (children[-1].tail or "") + trailing_text
    else:
        leading_text += trailing_text
    if preceding is None:
        parent.text = leading_text or None
    else:
        preceding.tail = leading_text or None


def _replace_content(element: html.HtmlElement, children: list[html.HtmlElement]):
    """Make `children` the whole content of `element`, text included."""
    element.text = None
    element[:] = children


def _remove_element(parent: html.HtmlElement, element: html.HtmlElement):
    """Remove `element` from `parent`, keeping the text that follows it."""
    preceding = element.getprevious()
    if element.tail:
        if preceding is None:
            parent.text = (parent.text or "") + element.tail
        else:
            preceding.tail = (preceding.tail or "") + element.tail
    parent.remove(element)


# htmx ignores an empty `hx-swap-oob`, and doesn't look inside a <template> until the second pass.
_OOB = "@hx-swap-oob != '' or @data-hx-swap-oob != ''"
_OOB_OUTSIDE_TEMPLATES = f"descendant-or-self::*[{_OOB}][not(ancestor::template)]"
_TEMPLATES_LEFT_IN_THE_RESPONSE = (
    f"descendant-or-self::template[not(ancestor::template)][not(ancestor-or-self::*[{_OOB}])]"
)
_OOB_IN_THIS_TEMPLATE = f"descendant::*[{_OOB}][count(ancestor::template) = 1]"


def _split_selector_list(selector: str) -> list[str]:
    """Split `selector` at its commas outside `<…/>`, dropping an empty last part, as htmx does."""
    depth = 0
    commas = []
    for index, char in enumerate(selector):
        if char == "," and depth == 0:
            commas.append(index)
        elif char == "<":
            depth += 1
        elif char == "/" and selector[index + 1 : index + 2] == ">":
            depth -= 1
    parts = [
        selector[start + 1 : end]
        for start, end in zip([-1, *commas], [*commas, len(selector)], strict=False)
    ]
    return parts if parts[-1] else parts[:-1]


def _normalize_selector(selector: str) -> str:
    """Strip `selector`, and unwrap it from `<…/>`, as htmx does."""
    stripped = selector.strip()
    if stripped.startswith("<") and stripped.endswith("/>"):
        return stripped[1:-2]
    else:
        return stripped


def _parse_oob_value(oob: str) -> "tuple[_SwapStrategy, str | None]":
    """Split the value of `hx-swap-oob` into a swap strategy and a selector, as htmx does.

    The selector is None when the target is the element with the id of the swapped one.  A
    strategy htmx doesn't know is `innerHTML`, its default.

    """
    if oob == "true":
        return "outerHTML", None
    else:
        strategy, selector = oob.split(":", 1) if oob.find(":") > 0 else (oob, None)
        return _SWAP_STRATEGIES.get(strategy, "innerHTML"), selector


type _SwapStrategy = Literal[
    "outerHTML", "innerHTML", "afterbegin", "beforeend", "beforebegin", "afterend", "delete", "none"
]
_SWAP_STRATEGIES: Mapping[str, _SwapStrategy] = {
    strategy: strategy for strategy in get_args(_SwapStrategy.__value__)
}
