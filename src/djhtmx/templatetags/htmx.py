import random
import typing as t

from django import template
from django.core.signing import Signer
from django.template.base import Node, Parser, Token
from django.template.context import Context
from django.urls import reverse
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe

from .. import json, settings
from ..component import REGISTRY, Component, PydanticComponent, generate_id
from ..repo import Repository

register = template.Library()
signer = Signer()

unset = object()


@register.inclusion_tag(
    "htmx/headers.html",
    takes_context=True,
    name="htmx-headers",
)
def htmx_headers(context):
    """Loads all the necessary scripts to make this work

    Use this tag inside your `<header></header>`.
    """
    if context.get("request"):
        return {
            "enabled": True,
            "CSRF_HEADER_NAME": settings.CSRF_HEADER_NAME,
            "SCRIPT_URLS": settings.SCRIPT_URLS,
            "csrf_token": context.get("csrf_token"),
        }
    else:
        return {"enabled": False}


@register.filter(name="add_delay_jitter")
def add_delay_jitter(event, arg=None):
    """Add a random `delay:{jitter}ms` to an event.

    The optional argument is the bound of the jitter.  By default is "100,
    1000" (ms).  The format is always a string with one or two numbers.  If
    the format is not right, fallback to the default.

    Example usage:

    ```
      <div {% on 'load'|add_htmx_jitter:'2000, 30000' 'render' %} ></div>
    ```

    """
    if not arg:
        arg = "100, 1000"
    try:
        min_arg, max_arg = arg.split(",", 1)
        min_ = int(min_arg.strip())
        max_ = int(max_arg.strip())
    except ValueError:
        min_, max_ = 100, 1000
    jitter = random.randint(min_, max_)
    return format_html(f"{event} {{}}", f"delay:{jitter}ms")


@register.simple_tag(takes_context=True)
def htmx(
    context,
    _name: str,
    _state: dict[str, t.Any] = None,
    *,
    lazy: t.Literal["once"] | bool = False,
    **state,
):
    """Inserts an HTMX Component.

    Pass the component name and the initial state:

        ```html
        {% htmx 'AmazinData' data=some_data %}
        ```
    """
    state = (_state or {}) | state
    if _name in REGISTRY:
        # PydanticComponent
        state |= {"lazy": lazy is True}
        repo = context.get("htmx_repo") or Repository.from_request(context["request"])
        component = repo.build(_name, state)
        return repo.render_html(component, lazy=lazy if isinstance(lazy, bool) else False)
    else:
        # Legacy Component
        id = state.pop("id", None) or generate_id()
        component = Component._build(_name, context["request"], id, state)
        return mark_safe(component._render())


@register.simple_tag(takes_context=True, name="hx-tag")
def hx_tag(context: Context):
    """Adds initialziation data to your root component tag.

    When your component starts, put it there:

        ```html
        {% load htmx %}
        <div {% hx-tag %}>
          ...
        </div>
        ```
    """
    component: Component | PydanticComponent = context["this"]
    if isinstance(component, PydanticComponent):
        oob = context.get("hx_oob")
        context["hx_oob"] = False
        attrs = {
            "id": component.id,
            "hx-swap-oob": "true" if oob else None,
        }
        if context.get("hx_lazy"):
            context["hx_lazy"] = False
            jitter = random.randint(100, 1000)
            attrs |= {
                "hx-trigger": f"revealed delay:{jitter}ms",
                "hx-get": event_url(component, "render"),
                "hx-headers": json.dumps({"HX-Session": context["htmx_repo"].session_signed_id}),
            }
    else:
        attrs = {
            "id": component.id,
            "hx-target": "this",
            "hx-boost": "false",
            "hx-post": event_url(component, "render"),
            "hx-trigger": "render",
            "hx-headers": json.dumps({
                "X-Component-State": signer.sign(component._state_json),
            }),
        }
    return format_html_attrs(attrs)


@register.simple_tag(takes_context=True)
def oob(context: Context):
    oob = context.get("hx_oob")
    context["hx_oob"] = False
    return format_html_attrs({"hx-swap-oob": "true" if oob else None})


@register.simple_tag(takes_context=True)
def on(
    context,
    _trigger,
    _event_handler=None,
    hx_include: str = None,
    **kwargs,
):
    """Binds an event to a handler

    If no trigger is provided, it assumes the default one by omission, in this
    case `click`, for an input is `change`:

        ```html
        <button {% on 'inc' %}>+</button>
        ```

    You can pass it explicitly:

        ```html
        <button {% on 'click' 'inc' %}>+</button>
        ```

    You can also pass explicit arguments:

        ```html
        <button {% on 'click' 'inc' amount=2 %}>+2</button>
        ```

    Remember that together with the explicit arguments, all fields with a
    `name` are passed as implicit arguments to your event handler.

    If you wanna do more advanced stuff read:
    [hx-trigger](https://htmx.org/attributes/hx-trigger/)

    """
    if not _event_handler:
        _event_handler = _trigger
        _trigger = None

    component: Component | PydanticComponent = context["this"]

    if settings.DEBUG:
        assert callable(
            getattr(component, _event_handler, None)
        ), f"{type(component).__name__}.{_event_handler} event handler not found"

    attrs = {
        "hx-post": event_url(component, _event_handler),
        "hx-trigger": _trigger,
        "hx-vals": json.dumps(kwargs) if kwargs else None,
        "hx-include": hx_include or f"#{component.id} [name]",
    }
    if isinstance(component, PydanticComponent):
        attrs |= {
            "hx-swap": "none",
            "hx-headers": json.dumps({"HX-Session": context["htmx_repo"].session_signed_id}),
        }
    return format_html_attrs(attrs)


def format_html_attrs(attrs: dict[str, t.Any]):
    return format_html_join(
        "\n",
        '{}="{}"',
        [(k, v) for k, v in attrs.items() if v is not None],
    )


def event_url(component: PydanticComponent | Component, event_handler: str):
    return reverse(
        f"djhtmx.{type(component).__name__}",
        kwargs={
            "component_id": component.id,
            "event_handler": event_handler,
        },
    )


# Shortcuts and helpers

_json_script_escapes = {
    ord(">"): "\\u003E",
    ord("<"): "\\u003C",
    ord("&"): "\\u0026",
}


@register.filter(name="safe_json")
def safe_json(obj):
    return mark_safe(json.dumps(obj).translate(_json_script_escapes))


@register.tag()
def cond(parser: Parser, token: Token):
    """Prints some text conditionally

        ```html
        {% cond {'works': True, 'does not work': 1 == 2} %}
        ```
    Will output 'works'.
    """
    dict_expression = token.contents[len("cond ") :]
    return CondNode(dict_expression)


@register.tag(name="class")
def class_cond(parser: Parser, token: Token):
    """Prints classes conditionally

    ```html
    <div {% class {'btn': True, 'loading': loading, 'falsy': 0} %}></div>
    ```

    If `loading` is `True` will print:

    ```html
    <div class="btn loading"></div>
    ```
    """
    dict_expression = token.contents[len("class ") :]
    return ClassNode(dict_expression)


class CondNode(Node):
    def __init__(self, dict_expression):
        self.dict_expression = dict_expression

    def render(self, context: template.Context):
        terms = eval(self.dict_expression, context.flatten())  # type: ignore
        return " ".join(term for term, ok in terms.items() if ok)


class ClassNode(CondNode):
    def render(self, *args, **kwargs):
        text = super().render(*args, **kwargs)
        return f'class="{text}"'
