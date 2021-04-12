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

    @classmethod
    def _build(cls, _component_name, request, id, state):
        klass = cls._all[_component_name]
        state = dict(klass._constructor_model.parse_obj(state), id=id)
        return klass(request=request, **state)

    def __init__(self, request, id: str = None):
        self.request = request
        self.id = id
        self._destroyed = False
        self._headers = {}
        self._triggers = {}

    @cached_property
    def user(self):
        return getattr(self.request, 'user', AnonymousUser())

    @property
    def _state_json(self):
        return self._constructor_model(**self._state).json()

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
        self._triggers.setdefault('hxSendEvent', []).append({
            'target': target,
            'event': event,
        })

    def render(self):
        if self._triggers:
            self._headers['HX-Trigger'] = json.dumps(self._triggers)
        return HttpResponse(self._render(), headers=self._headers)

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
