from collections import defaultdict
from itertools import chain

from django.contrib.auth.models import AnonymousUser
from django.http import HttpRequest, HttpResponse
from django.shortcuts import resolve_url
from django.template.loader import get_template, select_template
from django.utils.functional import cached_property
from pydantic import validate_arguments

from . import json


class Component:
    template_name = ''
    template = None
    _all = {}
    _urls = {}
    _name = ...

    _pydantic_config = {'arbitrary_types_allowed': True}

    def __init_subclass__(cls, name=None, public=True):
        if public:
            name = name or cls.__name__
            cls._all[name] = cls
            cls._name = name

        for attr_name in vars(cls):
            attr = getattr(cls, attr_name)
            if (
                attr_name == '__init__'
                or not attr_name.startswith('_')
                and attr_name.islower()
                and callable(attr)
            ):
                setattr(
                    cls,
                    attr_name,
                    validate_arguments(config=cls._pydantic_config)(attr),
                )

        return super().__init_subclass__()

    @classmethod
    def _build(cls, _component_name, request, id, state):
        return cls._all[_component_name](**dict(state, id=id, request=request))

    def __init__(self, request: HttpRequest, id: str = None):
        self.request = request
        self.id = id
        self._destroyed = False
        self._headers = {}
        self._triggers = Triggers()
        self._oob = []

    @cached_property
    def user(self):
        return getattr(self.request, 'user', AnonymousUser())

    @property
    def _state_json(self) -> str:
        return json.dumps(self._state)

    @property
    def _state(self) -> dict:
        return {
            name: getattr(self, name)
            for name in self.__init__.model.__fields__
            if hasattr(self, name)
        }

    def destroy(self):
        self._destroyed = True

    def redirect(self, url, **kwargs):
        self.redirect_raw_url(resolve_url(url, **kwargs))

    def redirect_raw_url(self, url):
        self._headers["HX-Redirect"] = url

    def push_url(self, url, **kwargs):
        self.push_raw_url(resolve_url(url, **kwargs))

    def push_raw_url(self, url):
        self._headers["HX-Push"] = url

    def _send_event(self, target, event):
        self._triggers.after_swap(
            'hxSendEvent',
            {
                'target': target,
                'event': event,
            },
        )

    def _focus(self, selector):
        self._triggers.after_settle('hxFocus', selector)

    def render(self):
        response = HttpResponse(self._render())
        for key, value in (self._headers | self._triggers.headers).items():
            response[key] = value
        return response

    def _render(self, hx_swap_oob=False):
        if self._destroyed:
            html = ''
        else:
            template = self._get_template()
            html = template.render(
                self._get_context(hx_swap_oob),
                request=self.request,
            )
            html = html.strip()

        if self._oob:
            html = '\n'.join(
                chain([html], [c._render(hx_swap_oob=True) for c in self._oob])
            )

        return html

    def _also_render(self, component, **kwargs):
        self._oob.append(component(request=self.request, **kwargs))

    def _get_template(self):
        if not self.template:
            if isinstance(self.template_name, (list, tuple)):
                self.template = select_template(self.template_name)
            else:
                self.template = get_template(self.template_name)
        return self.template

    def _get_context(self, hx_swap_oob):
        return dict(
            {
                attr: getattr(self, attr)
                for attr in dir(self)
                if not attr.startswith('_')
            },
            this=self,
            hx_swap_oob=hx_swap_oob,
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
