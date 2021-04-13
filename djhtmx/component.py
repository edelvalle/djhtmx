from collections import defaultdict

from django.http import HttpResponse
from django.shortcuts import resolve_url
from django.contrib.auth.models import AnonymousUser
from django.template.loader import get_template, select_template
from django.utils.functional import cached_property
from django.utils.safestring import mark_safe

from . import json
from .introspection import get_model


class Component:
    template_name = ''
    template = None
    _all = {}
    _urls = {}
    _name = ...

    def __init_subclass__(cls, name=None, public=True):
        if public:
            name = name or cls.__name__
            cls._all[name] = cls
            cls._name = name

        cls._models = {}
        for attr_name in dir(cls):
            attr = getattr(cls, attr_name)
            if (not attr_name.startswith('_')
                    and attr_name.islower()
                    and callable(attr)):
                cls._models[attr_name] = get_model(attr, ignore=['self'])

        cls._constructor_model = get_model(cls, ignore=['request'])
        cls._constructor_params = set(
            cls._constructor_model.schema()['properties']
        )
        return super().__init_subclass__()

    def __init__(self, request, id: str = None):
        self.request = request
        self.id = id
        self._destroyed = False
        self._headers = {}
        self._triggers = Triggers()

    @cached_property
    def user(self):
        return getattr(self.request, 'user', AnonymousUser())

    @property
    def _state_json(self):
        return json.dumps(self._state)

    @property
    def _state(self):
        return {
            name: value
            for name, value in vars(self).items()
            if name in self._constructor_params
        }

    def destroy(self):
        self._destroyed = True

    def redirect(self, url, **kwargs):
        self._headers['HX-Redirect'] = resolve_url(url, **kwargs)

    def push_url(self, url, **kwargs):
        self._headers['HX-Push'] = resolve_url(url, **kwargs)

    def _send_event(self, target, event):
        self._triggers.trigger('hxSendEvent', {
            'target': target,
            'event': event,
        })

    def _focus(self, selector):
        self._triggers.after_settle('hxFocus', selector)

    def render(self):
        response = HttpResponse(self._render())
        for key, value in (self._headers | self._triggers.headers).items():
            response[key] = value
        return response

    def _render(self):
        if self._destroyed:
            html = ''
        else:
            template = self._get_template()
            html = template.render(self._get_context(), request=self.request)
            html = html.strip()
        return mark_safe(html)

    def _get_template(self):
        if not self.template:
            if isinstance(self.template_name, (list, tuple)):
                self.template = select_template(self.template_name)
            else:
                self.template = get_template(self.template_name)
        return self.template

    def _get_context(self):
        return dict(
            {
                attr: getattr(self, attr)
                for attr in dir(self)
                if not attr.startswith('_')
            },
            this=self,
        )


class Triggers:
    def __init__(self):
        self._triggers = defaultdict(list)
        self._after_swap = defaultdict(list)
        self._after_settle = defaultdict(list)

    def trigger(self, name, what=None):
        self._triggers[name].append(what)

    def after_swap(self, name, what=None):
        self._after_swap[name].append(what)

    def after_settle(self, name, what=None):
        self._after_settle[name].append(what)

    @property
    def headers(self):
        headers = [
            ('HX-Trigger', self._triggers),
            ('HX-Trigger-After-Swap', self._after_swap),
            ('HX-Trigger-After-Settle', self._after_settle),
        ]
        return {header: json.dumps(value) for header, value in headers if value}
