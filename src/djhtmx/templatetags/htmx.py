import typing as t
from uuid import uuid4

from django import template
from django.core.signing import Signer
from django.template.base import Node, Parser, Token
from django.urls import reverse
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe

from .. import json, settings
from ..component import REGISTRY, Component, PydanticComponent, Repository

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
    return {
        "CSRF_HEADER_NAME": settings.CSRF_HEADER_NAME,
        "SCRIPT_URLS": settings.SCRIPT_URLS,
        "csrf_token": context.get("csrf_token"),
    }


@register.simple_tag(takes_context=True)
def htmx(context, _name: str, **state):
    """Inserts an HTMX Component.

    Pass the component name and the initial state:

        ```html
        {% htmx 'AmazinData' data=some_data %}
        ```
    """
    repo = Repository.from_request(
        context["request"],
        states_by_id={},
        subscriptions_by_id={},
    )
    if _name in REGISTRY:
        # PydanticComponent
        component = repo.build(_name, state)
        return repo.render_html(component)
    else:
        # Legacy Component
        if "id" in state:
            id = state.pop("id")
        else:
            id = f"hx-{uuid4().hex}"
        component = Component._build(_name, context["request"], id, state)
        return mark_safe(component._render())


@register.simple_tag(takes_context=True, name="hx-tag")
def hx_tag(context, swap: str = "outerHTML"):
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
        attrs = {
            "id": component.id,
            "hx-swap": swap,
            "hx-swap-oob": oob,
            "data-hx-state": signer.sign(component.model_dump_json()),
            "data-hx-subscriptions": (
                ",".join(subscriptions)
                if (subscriptions := component._get_all_subscriptions())
                else None
            ),
        }
        return format_html_attrs(attrs)
    else:
        html = [
            'id="{id}"',
            'hx-post="{url}"',
            'hx-trigger="render"',
            'hx-headers="{headers}"',
        ]

        if context.get("hx_swap_oob"):
            html.append('hx-swap-oob="true"')
        else:
            html.append(f'hx-swap="{swap}"')

        component = t.cast(Component, context["this"])
        return format_html(
            " ".join(html),
            id=context["id"],
            url=event_url(component, "render"),
            headers=json.dumps({
                "X-Component-State": signer.sign(component._state_json),
            }),
        )


@register.simple_tag(takes_context=True)
def on(
    context,
    _trigger,
    _event_handler=None,
    hx_target: str | object | None = unset,
    hx_include: str | object | None = unset,
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
        "hx-target": f"#{component.id}" if hx_target is unset else hx_target,
        "hx-include": (f"#{component.id} [name]" if hx_include is unset else hx_include),
        "hx-vals": json.dumps(kwargs) if kwargs else None,
    }
    return format_html_attrs(attrs)


def format_html_attrs(attrs: dict[str, t.Any]):
    return format_html_join(
        " ",
        '{}="{}"',
        [(k, v) for k, v in attrs.items() if v is not None],
    )


def event_url(component: PydanticComponent | Component, event_handler: str):
    if isinstance(component, PydanticComponent):
        component_name = type(component).__name__
        return reverse(
            f"djhtmx.{component_name}",
            kwargs={
                "component_id": component.id,
                "event_handler": event_handler,
            },
        )
    else:
        return reverse(
            "djhtmx.legacy_endpoint",
            kwargs={
                "component_name": type(component).__name__,
                "component_id": component.id,
                "event_handler": event_handler,
            },
        )


# Shortcuts and helpers


@register.filter()
def concat(prefix, suffix):
    return f"{prefix}{suffix}"


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
