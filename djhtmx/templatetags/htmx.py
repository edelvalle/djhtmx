from uuid import uuid4

from django import template
from django.core.signing import Signer
from django.utils.html import format_html
from django.urls import reverse
from django.conf import settings

from .. import json
from ..component import Component

register = template.Library()

CSRF_HEADER = settings.CSRF_HEADER_NAME[5:].replace('_', '-')


@register.inclusion_tag('htmx/headers.html')
def htmx_headers():
    return {}


@register.simple_tag(takes_context=True)
def htmx(context, _name, id=None, **state):
    id = id or f'hx-{uuid4().hex}'
    component = Component._build(_name, context['request'], id, state)
    return component._render()


@register.simple_tag(takes_context=True, name='hx-header')
def hx_header(context):
    component = context['this']
    headers = {
        CSRF_HEADER: str(context['csrf_token']),
        'X-Component-State': Signer().sign(component._state_json),
    }
    return format_html(
        'id="{id}" '
        'hx-post="{url}" '
        'hx-trigger="render" '
        'hx-swap="outerHTML" '
        'hx-ext="morphdom-swap" '
        'hx-headers="{headers}"',
        id=context['id'],
        url=event_url(component, 'render'),
        headers=json.dumps(headers),
    )


@register.simple_tag(takes_context=True)
def on(context, _trigger, _event_handler=None, **kwargs):
    if not _event_handler:
        _event_handler = _trigger
        _trigger = None

    component = context['this']

    assert callable(getattr(component, _event_handler, None)), \
        f'{component._name}.{_event_handler} event handler not found'

    html = ' '.join(filter(None, [
        'hx-post="{url}"'
        'hx-target="#{id}"',
        'hx-include="#{id} [name]"',
        'hx-trigger="{trigger}"' if _trigger else None,
        'hx-vals="{vals}"' if kwargs else None,
    ]))

    return format_html(
        html,
        trigger=_trigger,
        url=event_url(component, _event_handler),
        id=context['id'],
        vals=json.dumps(kwargs) if kwargs else None,
    )


def event_url(component, event_handler):
    return reverse(
        'djhtmx.endpoint',
        kwargs={
            'component_name': component._name,
            'id': component.id,
            'event_handler': event_handler
        }
    )
