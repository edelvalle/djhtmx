from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from contextlib import AbstractContextManager, contextmanager
from functools import reduce
from typing import Any, cast, overload
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
    Render,
    ReplaceURL,
    ScrollIntoView,
    SendHtml,
    SkipRender,
)
from .component import HtmxComponent
from .introspection import parse_request_data
from .repo import Repository, Session, signer
from .utils import get_params

__all__ = ("Htmx",)


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
    def assertEmits[E](self, event_class: type[E], *, with_sse: bool = True) -> Iterator[E]:
        """Assert that an `event_class` event is emitted inside the block.

        The block is what is watched, not one command: every `Emit` a handler yields inside it
        counts, the one from the handler under test and any a listener raised while it reacted to
        that one.  The value the block receives stands for the event and can be read as soon as it
        exists::

            with self.htmx.assertEmits(FeedbackMessage) as event:
                self.htmx.send(editor.rebuild_the_items)
                self.assertIn("Open an item first", event.body)

        Reading an attribute of it before any such event was emitted fails the test, and so does
        leaving the block having emitted none.  When several match it stands for the first.

        The handlers the SSE drain wakes -- `dispatch_event` runs it before returning -- are
        watched as well; pass `with_sse=False` to watch only what the browser event itself set
        off.

        """
        with self._watching(Emit, event_class, with_sse=with_sse) as event:
            yield cast(E, event)

    @overload
    def assertYields[C: Command](
        self, command_class: type[C], *, with_sse: bool = True
    ) -> AbstractContextManager[C]: ...

    @overload
    def assertYields(
        self, command_class: None, *, with_sse: bool = True
    ) -> AbstractContextManager[None]: ...

    @contextmanager
    def assertYields(
        self, command_class: type[Any] | None, *, with_sse: bool = True
    ) -> Iterator[Any]:
        """Assert that a `command_class` command is yielded inside the block.

        The sibling of `assertEmits`:meth: one level down: it watches the commands handlers yield
        instead of the events they emit.  Any command of that class yielded inside the block
        counts -- the handler under test's, and that of any handler the cascade woke up::

            with self.htmx.assertYields(Redirect) as command:
                self.htmx.send(editor.save_and_leave)
                self.assertEqual(command.url, self.owner_url)

        `None` asserts the other side, that no handler yielded anything at all::

            with self.htmx.assertYields(None):
                self.htmx.send(editor.open_the_item, item=self.item)

        Only what a handler yields is watched, so the default `Render` djhtmx adds for a handler
        that yielded nothing of its own -- djhtmx's command, not the handler's -- is not what
        makes `assertYields(None)` fail.

        See `assertEmits`:meth: about reading the value inside the block, about several matches,
        and about `with_sse`.

        """
        with self._watching(command_class, None, with_sse=with_sse) as command:
            yield command

    @contextmanager
    def _watching(
        self,
        command_class: type[Any] | None,
        event_class: type[Any] | None,
        *,
        with_sse: bool,
    ) -> Iterator[Any]:
        """Run the block with a recorder installed, and assert on the tape it left."""
        with CommandProcessor._install_recorder() as recorder:
            watch = _Watch(recorder, command_class, event_class, with_sse=with_sse)
            yield None if command_class is None else watch
            assert watch.matched(), watch.failure()

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


class _Watch:
    """What a watched block expects of the commands its handlers yield.

    It is also the value the block receives: attribute access resolves against what has been
    yielded *so far*, so an assertion can sit right after the `Htmx.send`:meth: that produces it,
    inside the block.  While nothing matches, any attribute access fails the test with the same
    message leaving the block would give.

    """

    def __init__(
        self,
        recorder: CommandRecorder,
        command_class: type[Any] | None,
        event_class: type[Any] | None,
        *,
        with_sse: bool,
    ):
        self._recorder = recorder
        self._start = len(recorder.commands)
        self._command_class = command_class
        self._event_class = event_class
        self._with_sse = with_sse

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            # Let introspection (copy, pickle, a test runner formatting a failure) fail its own
            # look-ups the way it expects instead of failing the test being written.
            raise AttributeError(name)
        return getattr(self._first(), name)

    def __eq__(self, other: object) -> bool:
        return self._first() == other

    def __repr__(self) -> str:
        if matches := self.matches():
            return repr(matches[0])
        else:
            return f"<{type(self).__name__}: {self.failure()}>"

    def matched(self) -> bool:
        """Whether the block kept what it promised."""
        if self._command_class is None:
            return not self.recorded()
        else:
            return bool(self.matches())

    def matches(self) -> list[Any]:
        """The events, or the commands, yielded inside the block that the watch is about."""
        commands = [recorded.command for recorded in self.recorded()]
        if self._event_class is not None:
            return [
                command.event
                for command in commands
                if isinstance(command, Emit) and isinstance(command.event, self._event_class)
            ]
        elif self._command_class is not None:
            return [command for command in commands if isinstance(command, self._command_class)]
        else:
            return []

    def recorded(self) -> list[RecordedCommand]:
        """Everything the handlers yielded inside the block, watched or not."""
        return self._recorder.since(self._start, with_sse=self._with_sse)

    def failure(self) -> str:
        """The message for a block that did not keep its promise."""
        yielded = _describe(self.recorded())
        if self._command_class is None:
            return f"Expected nothing to be yielded inside the block, but got: {yielded}"
        elif self._event_class is not None:
            return f"No {self._event_class.__name__} was emitted inside the block; got: {yielded}"
        else:
            return f"No {self._command_class.__name__} was yielded inside the block; got: {yielded}"

    def _first(self) -> Any:
        if matches := self.matches():
            return matches[0]
        else:
            raise AssertionError(self.failure())


def _describe(recorded: Iterable[RecordedCommand]) -> str:
    """One line naming each command in `recorded` and the handler that yielded it."""
    described = ", ".join(
        f"{recorded_command.source} -> {_describe_command(recorded_command.command)}"
        for recorded_command in recorded
    )
    return described or "nothing"


def _describe_command(command: Command) -> str:
    """A command in one short piece of text, with no component state in it."""
    if isinstance(command, Emit):
        return f"Emit({command.event!r})"
    elif isinstance(command, Render | SkipRender):
        return f"{type(command).__name__}({command.component.hx_name}#{command.component.id})"
    else:
        return repr(command)
