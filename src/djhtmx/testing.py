from collections import defaultdict
from collections.abc import Callable, Iterable
from functools import reduce
from typing import Any, ParamSpec, TypeVar
from urllib.parse import urlparse

from django.contrib.auth.models import AnonymousUser
from django.test import Client
from lxml import html
from pygments import highlight
from pygments.formatters import TerminalTrueColorFormatter
from pygments.lexers import HtmlLexer

from . import json
from .commands import PushURL, ReplaceURL, SendHtml
from .component import Destroy, DispatchDOMEvent, Focus, HtmxComponent, Open, Redirect
from .introspection import parse_request_data
from .repo import Repository, Session, signer
from .utils import get_params

P = ParamSpec("P")
TPComponent = TypeVar("TPComponent", bound=HtmxComponent)


class Htmx:
    def __init__(self, client: Client):
        self.client = client

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

        self.repo = Repository(
            user=response.context.get("user") or AnonymousUser(),
            session=Session(session_id),
            params=get_params(self.query_string),
        )

    def get_component_by_type(self, component_type: type[TPComponent]) -> TPComponent:
        [component] = self.repo.get_components_by_names(component_type.__name__)
        return component  # type: ignore

    def get_components_by_type(self, component_type: type[TPComponent]) -> Iterable[TPComponent]:
        return self.repo.get_components_by_names(component_type.__name__)  # type: ignore

    def get_component_by_id(self, component_id: str):
        component = self.repo.get_component_by_id(component_id)
        assert isinstance(component, HtmxComponent)
        return component

    def type(self, selector: str | html.HtmlElement, text: str, clear=False):
        """Sets the value of an input, by "typing" in to it"""
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

    def send(self, method: Callable[P, Any], *args: P.args, **kwargs: P.kwargs):
        assert not args, "All parameters have to be passed by name"
        self.dispatch_event(method.__self__.id, method.__name__, kwargs)  # type: ignore

    def dispatch_event(self, component_id: str, event_handler: str, kwargs: dict[str, Any]):
        commands = self.repo.dispatch_event(component_id, event_handler, kwargs)
        navigate_to_url = None
        for command in commands:
            match command:
                case SendHtml(content):
                    incoming = html.fromstring(content)
                    oob: str = incoming.attrib["hx-swap-oob"]
                    if oob == "true":
                        target = self.dom.get_element_by_id(incoming.attrib["id"])
                        if parent := target.getparent():
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
                    else:
                        assert False, "Unknown swap strategy, please define it here"

                case Destroy(component_id):
                    target = self.dom.get_element_by_id(component_id)
                    if parent := target.getparent():
                        parent.remove(target)

                case Redirect(url) | Open(url):
                    navigate_to_url = url

                case PushURL(url) | ReplaceURL(url):
                    parsed_url = urlparse(url)
                    self.path = parsed_url.path
                    self.query_string = parsed_url.query

                case Focus() | DispatchDOMEvent():
                    pass

        if navigate_to_url:
            self.navigate_to(navigate_to_url)
