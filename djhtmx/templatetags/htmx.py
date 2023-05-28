import json
from uuid import uuid4

from django import template
from django.conf import settings
from django.core.signing import Signer
from django.template.base import Node, Parser, Token
from django.templatetags.static import static
from django.urls import reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from ..component import BaseHTMXComponent

register = template.Library()

# The name of the header without the 'HTTP_' prefix and replacing '_' with
# '-'.
CSRF_HEADER_NAME = settings.CSRF_HEADER_NAME[5:].replace('_', '-')


@register.inclusion_tag(
    'htmx/headers.html',
    takes_context=True,
    name='htmx-headers',
)
def htmx_headers(context):
    """Loads all the necessary scripts to make this work

    Use this tag inside your ``<header></header>``.

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
def htmx(context, _name, **state):
    """Inserts an HTMX Component.

    Pass the component name and the initial state::

        {% htmx 'AmazingData' data=some_data %}

    """
    state.setdefault('id', f'hx-{uuid4().hex}')
    request = context.get('request') or context['this']._meta.request
    component = BaseHTMXComponent._build(_name, request, state)
    return mark_safe(component._render())


@register.simple_tag(takes_context=True, name='hx-tag')
def hx_tag(context, **options):
    """Adds initialziation data to your root component tag.

    When your component starts, put it there::

        {% load htmx %}
        <div {% hx-tag %}>
          ...
        </div>

    """
    html = [
        'id="{id}"',
        'hx-post="{url}"',
        'hx-trigger="render"',
        'hx-headers="{headers}"',
    ]

    if context.get('hx_swap_oob'):
        html.append('hx-swap-oob="true"')
    else:
        swap = options.get('hx_swap', 'outerHTML')
        html.append(f'hx-swap="{swap}"')

    component = context['this']
    return format_html(
        ' '.join(html),
        id=context['id'],
        url=event_url(component, 'render'),
        headers=json.dumps(
            {
                'X-Component-State': Signer().sign(component.json()),
            }
        ),
    )


@register.simple_tag(takes_context=True)
def on(context, _trigger, _event_handler=None, **kwargs):
    """Binds an event to a handler

    If no trigger is provided, it assumes the default one by omission, in this
    case ``click``, for an input is ``change``::

        <button {% on 'inc' %}>+</button>

    You can pass it explicitly::

        <button {% on 'click' 'inc' %}>+</button>

    You can also pass explicit arguments::

        <button {% on 'inc' amount=2 %}>+2</button>

    Together with the explicit arguments, all fields with a ``name`` are
    passed as implicit arguments to your event handler.

    .. seealso::

       If you wanna do more advanced stuff read `hx-trigger
       <https://htmx.org/attributes/hx-trigger/>`_.

    """
    if _event_handler:
        trigger = _trigger
        event_handler = _event_handler
    else:
        trigger = None
        event_handler = _trigger

    component = context['this']

    assert callable(
        getattr(component, event_handler, None)
    ), f'{component._name}.{event_handler} event handler not found'

    html = ' '.join(
        filter(
            None,
            [
                'hx-post="{url}" ',
                'hx-target="#{id}" ',
                'hx-include="#{id} [name]" ',
                'hx-trigger="{trigger}" ' if trigger else None,
                'hx-vals="{vals}" ' if kwargs else None,
            ],
        )
    )

    return format_html(
        html,
        trigger=trigger,
        url=event_url(component, event_handler),
        id=context['id'],
        vals=json.dumps(kwargs) if kwargs else None,
    )


def event_url(component, event_handler):
    return reverse(
        'djhtmx.endpoint',
        kwargs={
            'component_name': component._name,
            'event_handler': event_handler,
        },
    )


# Shortcuts and helpers


@register.tag()
def cond(parser: Parser, token: Token):
    """Prints some text conditionally.

    ::

        {% cond {'works': True, 'does not work': 1 == 2} %}

    will output 'works'.
    """
    dict_expression = token.contents[len('cond ') :]
    return CondNode(dict_expression)


@register.tag(name='class')
def class_cond(parser: Parser, token: Token):
    """Prints classes conditionally

    ::
       <div {% class {'btn': True, 'loading': loading, 'falsy': 0} %}></div>

    If `loading` is `True` will print::

       <div class="btn loading"></div>

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


HTMX_VERSION = '1.6.1'


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
