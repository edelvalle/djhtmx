from django import template
from django.conf import settings
from django.core.signing import Signer
from django.template.base import Node, Parser, Token
from django.templatetags.static import static
from django.urls import reverse
from django.utils.html import format_html

from .. import json
from ..component import Component, Repository

register = template.Library()

CSRF_HEADER_NAME = settings.CSRF_HEADER_NAME[5:].replace('_', '-')


@register.inclusion_tag(
    'htmx/headers.html',
    takes_context=True,
    name='htmx-headers',
)
def htmx_headers(context):
    """Loads all the necessary scripts to make this work

    Use this tag inside your `<header></header>`.
    """
    cfg = HTMX_SCRIPTS_CFG()
    htmx_core_scripts = cfg[":core:"]
    htmx_extension_scripts = [
        script
        for extension in getattr(settings, 'HTMX_INSTALLED_EXTENSIONS', [])
        for script in cfg[extension]
    ]
    return {
        'csrf_header_name': CSRF_HEADER_NAME,
        'csrf_token': context.get('csrf_token'),
        'DEBUG': settings.DEBUG,
        'htmx_core_scripts': htmx_core_scripts,
        'htmx_extension_scripts': htmx_extension_scripts,
    }


@register.simple_tag(takes_context=True)
def htmx(context, _name: str, **state):
    """Inserts an HTMX Component.

    Pass the component name and the initial state:

        ```html
        {% htmx 'AmazinData' data=some_data %}
        ```
    """
    repo = context.get("htmx_repo") or Repository(context['request'])
    component = repo.build(_name, state)
    return repo.render_html(component)


@register.simple_tag(takes_context=True, name='hx-tag')
def hx_tag(context, **options):
    """Adds initialziation data to your root component tag.

    When your component starts, put it there:

        ```html
        {% load htmx %}
        <div {% hx-tag %}>
          ...
        </div>
        ```
    """
    html = [
        'id="{id}"',
        'hx-post="{url}"',
        'hx-include="#{id} [name]"',
        'hx-trigger="render"',
        'hx-swap="outerHTML"',
        'data-hx-state="{state}"',
    ]

    if context.get('hx_swap_oob'):
        html.append('hx-swap-oob="true"')

    component: Component = context['this']
    return format_html(
        ' '.join(html),
        id=component.id,
        url=event_url(component, 'render'),
        state=Signer().sign(
            component.model_dump_json(exclude=component._exclude_fields)
        ),
    )


@register.simple_tag(takes_context=True)
def on(context, _trigger, _event_handler=None, **kwargs):
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
        <button {% on 'inc' amount=2 %}>+2</button>
        ```

    Remember that together with the explicit arguments, all fields with a
    `name` are passed as implicit arguments to your event handler.

    If you wanna do more advanced stuff read:
    [hx-trigger](https://htmx.org/attributes/hx-trigger/)

    """
    if not _event_handler:
        _event_handler = _trigger
        _trigger = None

    component = context['this']

    assert callable(
        getattr(component, _event_handler, None)
    ), f'{type(component).__name__}.{_event_handler} event handler not found'

    html = ' '.join(
        filter(
            None,
            [
                'hx-post="{url}"',
                'hx-target="#{id}"',
                'hx-trigger="{trigger}"' if _trigger else None,
                'hx-vals="{vals}"' if kwargs else None,
            ],
        )
    )

    return format_html(
        html,
        id=component.id,
        trigger=_trigger,
        url=event_url(component, _event_handler),
        vals=json.dumps(kwargs) if kwargs else None,
    )


def event_url(component, event_handler):
    return reverse(
        'djhtmx.endpoint',
        kwargs={
            'component_name': type(component).__name__,
            'event_handler': event_handler,
        },
    )


# Shortcuts and helpers


@register.filter()
def concat(prefix, suffix):
    return f'{prefix}{suffix}'


@register.tag()
def cond(parser: Parser, token: Token):
    """Prints some text conditionally

        ```html
        {% cond {'works': True, 'does not work': 1 == 2} %}
        ```
    Will output 'works'.
    """
    dict_expression = token.contents[len('cond ') :]
    return CondNode(dict_expression)


@register.tag(name='class')
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
    dict_expression = token.contents[len('class ') :]
    return ClassNode(dict_expression)


class CondNode(Node):
    def __init__(self, dict_expression):
        self.dict_expression = dict_expression

    def render(self, context):
        terms = eval(self.dict_expression, context.flatten())
        return ' '.join(term for term, ok in terms.items() if ok)


class ClassNode(CondNode):
    def render(self, *args, **kwargs):
        text = super().render(*args, **kwargs)
        return f'class="{text}"'


HTMX_VERSION = '1.9.6'


# A map from a name (an extension or otherwise) to the list of statics that
# make it up.
#
# Yet this is a method to avoid calling `static` too early.
def HTMX_SCRIPTS_CFG():
    if _HTMX_SCRIPTS_CFG:
        return _HTMX_SCRIPTS_CFG
    else:
        res = {
            ':core:': [
                static(
                    f'htmx/{HTMX_VERSION}/htmx.js'
                    if getattr(settings, 'DEBUG', False)
                    else f'htmx/{HTMX_VERSION}/htmx.min.js'
                ),
                static('htmx/htmx-django.js'),
            ],
            'morphdom-swap': [
                static(
                    'htmx/vendors/morphdom/morphdom-umd.js'
                    if getattr(settings, 'DEBUG', False)
                    else 'htmx/vendors/morphdom/morphdom-umd.min.js'
                ),
                static(f'htmx/{HTMX_VERSION}/ext/morphdom-swap.js'),
            ],
        }
        for ext in _HTMX_BASIC_EXTENSIONS:
            res[ext] = [static(f'htmx/{HTMX_VERSION}/ext/{ext}.js')]
        _HTMX_SCRIPTS_CFG.update(res)
        return res


_HTMX_SCRIPTS_CFG = {}
_HTMX_BASIC_EXTENSIONS = (
    'debug',
    'ajax-header',
    'class-tools',
    'event-header',
    'include-vals',
    'json-enc',
    'method-override',
    'path-deps',
    'preload',
    'remove-me',
)
