import random
from collections.abc import Mapping
from typing import Any, Literal, cast

from django import template
from django.core.signing import Signer
from django.template.base import FilterExpression, Node, Parser, Token
from django.template.context import Context
from django.template.defaulttags import TemplateIfParser, TemplateLiteral
from django.template.exceptions import TemplateSyntaxError
from django.urls import reverse
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe

from .. import json, settings
from ..component import HtmxComponent
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
            "SCRIPT_URLS": settings.SCRIPT_URLS,
            "CSRF_HEADER_NAME": settings.CSRF_HEADER_NAME,
            "csrf_token": context.get("csrf_token", ""),
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
    _state: dict[str, Any] | None = None,
    *,
    lazy: Literal["once"] | bool = False,
    **state,
):
    """Inserts an HTMX Component.

    Pass the component name and the initial state:

        ```html
        {% htmx 'AmazinData' data=some_data %}
        ```
    """
    state = (_state or {}) | state
    repo: Repository = context["htmx_repo"]
    state |= {"lazy": lazy is True}

    # Extract parent component ID from context if available
    parent_id = getattr(context.get("this"), "id", None)

    component = repo.build(_name, state, parent_id=parent_id)
    return repo.render_html(
        component,
        lazy=lazy if isinstance(lazy, bool) else False,
    )


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
    component: HtmxComponent = context["this"]
    oob = context.get("hx_oob")
    context["hx_oob"] = False
    attrs = {
        "id": component.id,
        "hx-swap-oob": "true" if oob else None,
        "hx-headers": json.dumps({"HX-Session": context["htmx_repo"].session_signed_id}),
    }
    if context.get("hx_lazy"):
        context["hx_lazy"] = False
        jitter = random.randint(100, 1000)
        attrs |= {
            "hx-trigger": f"revealed delay:{jitter}ms",
            "hx-get": event_url(component, "render"),
        }
    if settings.DEBUG:
        attrs |= {"hx-name": component.hx_name}
    return format_html_attrs(attrs)


@register.simple_tag(takes_context=True)
def oob(context: Context, suffix: str):
    oob = context.get("hx_oob")
    context["hx_oob"] = False
    id = "-".join(filter(None, (context.get("id"), suffix)))
    return format_html_attrs({"id": id, "hx-swap-oob": "true" if oob else None})


@register.simple_tag(takes_context=True)
def on(
    context,
    _trigger,
    _event_handler=None,
    hx_include: str | None = None,
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

    component: HtmxComponent = context["this"]

    if settings.DEBUG:
        assert _event_handler in component._event_handler_params, (
            f"{type(component).__name__}.{_event_handler} event handler not found"
        )

    if not hx_include:
        has_implicit_params = bool(component._event_handler_params[_event_handler] - set(kwargs))
        if has_implicit_params:
            hx_include = f"#{component.id} [name]"

    attrs = {
        "hx-swap": "none",
        "hx-post": event_url(component, _event_handler),
        "hx-trigger": _trigger,
        "hx-vals": json.dumps(kwargs) if kwargs else None,
        "hx-include": hx_include,
    }
    return format_html_attrs(attrs)


def format_html_attrs(attrs: dict[str, Any]):
    return format_html_join(
        "\n",
        '{}="{}"',
        [(k, v) for k, v in attrs.items() if v is not None],
    )


def event_url(component: HtmxComponent, event_handler: str):
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


@register.tag(name="class")
def class_cond(parser: Parser, token: Token):
    """Prints classes conditionally

    ```html
    <div {% class 'btn': True, 'loading': loading, 'falsy': 0} %}></div>
    <div {% class 'btn' x == 'something', 'loading': y is None %}></div>
    ```

    If `loading` is `True` will print:

    ```html
    <div class="btn loading"></div>
    ```
    """
    bits = token.split_contents()[1:]
    classes: list[tuple[FilterExpression, list[str]]] = []

    is_class_name = True
    for bit in bits:
        if is_class_name:
            if bit.endswith(":"):
                classes.append((FilterExpression(bit[:-1], parser), []))
                is_class_name = False
                continue
            else:
                raise TemplateSyntaxError(f"Expected colon (:) after: {bit}")
        else:
            if bit.endswith(","):
                expr = bit[:-1]
                is_class_name = True
            else:
                expr = bit
        classes[-1][1].append(expr)

    return ClassNode([
        (TemplateIfParser(parser, expr).parse(), class_name) for class_name, expr in classes
    ])


class ClassNode(Node):
    def __init__(self, condition_and_classes: list[tuple[TemplateLiteral, FilterExpression]]):
        self.condition_and_classes = condition_and_classes

    def render(self, context: Context):
        class_names = [
            class_name.resolve(cast(Mapping[str, Any], context))
            for condition, class_name in self.condition_and_classes
            if condition.eval(context)  # type: ignore
        ]
        return format_html_attrs({"class": " ".join(class_names) or None})
