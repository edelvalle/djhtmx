from collections import UserList, defaultdict
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from functools import reduce
from typing import Any, overload
from urllib.parse import urlparse
from warnings import deprecated

from asgiref.sync import async_to_sync
from django.contrib.auth.models import AnonymousUser
from django.test import Client
from lxml import html
from pygments import highlight
from pygments.formatters import TerminalTrueColorFormatter
from pygments.lexers import HtmlLexer

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
        self.client = client

    @property
    def url(self) -> str:
        return f"{self.path}?{self.query_string}".rstrip("?")

    def navigate_to(self, url: str, *args, **kwargs):
        kwargs.setdefault("follow", True)
        response = self.client.get(url, *args, **kwargs)
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

        self.user = response.context.get("user") or AnonymousUser()
        self.repo = Repository(
            user=self.user,
            session=Session(session_id),
            params=get_params(self.query_string),
        )

    def get_component_by_type[C: HtmxComponent](self, component_type: type[C]) -> C:
        [component] = self.repo.get_components_by_names(component_type.__name__)
        return component  # type: ignore

    def get_components_by_type[C: HtmxComponent](self, component_type: type[C]) -> Iterable[C]:
        return self.repo.get_components_by_names(component_type.__name__)  # type: ignore

    def get_component_by_id(self, component_id: str):
        component = self.repo.get_component_by_id(component_id)
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
        assert not args, "All parameters have to be passed by name"
        self.dispatch_event(method.__self__.id, method.__name__, kwargs)  # type: ignore

    def dispatch_event(self, component_id: str, event_handler: str, kwargs: dict[str, Any]):
        commands = self.repo.dispatch_event(component_id, event_handler, kwargs)
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
                    self.path = parsed_url.path
                    self.query_string = parsed_url.query

                case Focus() | ScrollIntoView() | DispatchDOMEvent():
                    pass

        if sse_html := async_to_sync(self._render_sse_events)():
            self._apply_oob_html(sse_html)

        if navigate_to_url:
            self.navigate_to(navigate_to_url)

    @contextmanager
    def assertEmits[E](
        self,
        event_class: type[E],
        *,
        with_sse: bool = True,
    ) -> "Iterator[CapturedEvents[E]]":
        """Assert that at least one `event_class` event is emitted inside the block.

        The block receives a `CapturedEvents`:class: standing for the events of that class.  If no
        event was found it will raise at exit.

        Example::

            with self.htmx.assertEmits(FeedbackMessage) as captured:
                self.htmx.send(editor.rebuild_the_items)
            event = captured.get_event()
            self.assertIn("Open an item first", event.body)

        The argument `with_sse` has the same meaning as in `assertYields`:meth:.

        """
        with self.assertYields(Emit, with_sse=with_sse) as emits:
            captured = CapturedEvents(emits, event_class)
            yield captured
            captured.get_event()  # This is the assertion that at least some event was emitted.

    @overload
    def assertYields[C: Command](
        self,
        command_class: type[C],
        *,
        with_sse: bool = True,
    ) -> "AbstractContextManager[CapturedCommands[C]]": ...

    @overload
    def assertYields(
        self,
        command_class: None,
        *,
        with_sse: bool = True,
    ) -> AbstractContextManager[None]: ...

    @contextmanager
    def assertYields[C: Command](
        self,
        command_class: type[C] | None,
        *,
        with_sse: bool = True,
    ) -> Iterator[Any]:
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

        .. rubric:: SSE emits

        With `with_sse`, the default, the capture also holds what the components yield in
        response to the session's SSE events.  Pass `with_sse=False` for a capture of only what
        the event sent from the browser set off.

        """
        with CommandProcessor._install_recorder() as recorder:
            captured = CapturedCommands(recorder, command_class, with_sse=with_sse)
            yield None if command_class is None else captured
            assert captured.is_satisfied(), captured.get_failure_message()

    async def _render_sse_events(self):
        from .sse import render_sse_events

        return await render_sse_events(self.repo.session.id, self.user)

    def _apply_oob_html(self, content: str):
        fragments = [
            fragment
            for fragment in html.fragments_fromstring(content)
            if isinstance(fragment, html.HtmlElement)
        ]
        for incoming in fragments:
            oob: str = incoming.attrib["hx-swap-oob"]
            if oob == "true":
                target = self.dom.get_element_by_id(incoming.attrib["id"])
                parent = target.getparent()
                if parent is not None:
                    parent.replace(target, incoming)
            elif oob.startswith("beforeend: "):
                target_selector = oob.removeprefix("beforeend: ")
                [target] = self.dom.cssselect(target_selector)
                target.append(incoming.getchildren()[0])
            elif oob.startswith("afterbegin: "):
                target_selector = oob.removeprefix("afterbegin: ")
                [target] = self.dom.cssselect(target_selector)
                target.insert(0, incoming.getchildren()[0])
            elif oob.startswith("afterend: "):
                target_selector = oob.removeprefix("afterend: ")
                [target] = self.dom.cssselect(target_selector)
                target.addnext(incoming.getchildren()[0])
            elif oob.startswith("beforebegin: "):
                target_selector = oob.removeprefix("afterend: ")
                [target] = self.dom.cssselect(target_selector)
                target.addprevious(incoming.getchildren()[0])
            elif oob == "delete":
                target = self.dom.get_element_by_id(incoming.attrib["id"])
                parent = target.getparent()
                if parent is not None:
                    parent.remove(target)
            else:
                assert False, "Unknown swap strategy, please define it here"

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

    This is what `Htmx.assertYields`:meth: hands to its block.  It is a list, and a live one: the
    block receives it before anything has been yielded, and every read answers with what has been
    yielded so far, which is why a test reads it after the block rather than inside it -- see the
    note in `Htmx.assertYields`:meth:.

    Its items are the commands the handlers yielded, so reading their attributes is checked like
    any other attribute access.  `get_recorded`:meth: answers with everything the handlers yielded,
    captured or not, which is what a failure reports.

    Mutating the list has no effect.

    """

    def __init__(
        self,
        recorder: CommandRecorder,
        command_class: type[C] | None,
        *,
        with_sse: bool,
    ):
        self._recorder = recorder
        self._start = len(recorder.commands)
        self._command_class = command_class
        self._with_sse = with_sse

    # `UserList` declares `data` as a plain list it owns, hence the ignore: here it is derived
    # instead, which is what makes the list live -- the block is handed this object before its
    # first command exists, and `UserList` reads every operation off `data`.
    @property
    def data(self) -> list[C]:  # type: ignore[override]
        if self._command_class is None:
            return []
        else:
            return [
                recorded.command
                for recorded in self.get_recorded()
                if isinstance(recorded.command, self._command_class)
            ]

    def get_recorded(self) -> list[RecordedCommand]:
        """Answer with everything the handlers yielded inside the block, captured or not."""
        return self._recorder.since(self._start, with_sse=self._with_sse)

    def is_satisfied(self) -> bool:
        """Whether the block kept what it promised."""
        if self._command_class is None:
            return not self.get_recorded()
        else:
            return bool(self.data)

    def get_failure_message(self) -> str:
        """Build the message for a block that did not keep its promise."""
        yielded = ", ".join(recorded.describe() for recorded in self.get_recorded()) or "nothing"
        if self._command_class is None:
            return f"Expected nothing to be yielded inside the block, but got: {yielded}"
        else:
            # A command lives in `djhtmx.commands`, so its module tells a reader nothing; an
            # event's does, since two applications can name an event alike.
            command = get_fqn(self._command_class).removeprefix("djhtmx.commands.")
            return f"No {command} was yielded inside the block; got: {yielded}"
