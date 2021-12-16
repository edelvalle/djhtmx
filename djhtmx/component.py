import json
import typing as t
from collections import defaultdict
from itertools import chain

from django.contrib.auth.models import AnonymousUser
from django.db import models
from django.http import HttpResponse
from django.shortcuts import resolve_url
from django.template.loader import get_template, select_template
from django.utils.functional import cached_property
from pydantic import BaseModel, validate_arguments

from .cache import CacheMixin
from .errors import ComponentNotFound
from .tracing import sentry_span


class ComponentMeta:
    def __init__(self, request):
        self.request = request
        self.template = None
        self.headers = {}
        self.triggers = Triggers()
        self.oob = []
        self.destroyed = False

    @cached_property
    def user(self):
        return getattr(self.request, 'user', AnonymousUser())

    @property
    def all_headers(self):
        return self.headers | self.triggers.headers


class BaseHTMXComponent(BaseModel):
    _all: t.ClassVar[dict[str, t.Type['BaseHTMXComponent']]] = {}
    _pydantic_config = {'arbitrary_types_allowed': True}

    class Config:
        arbitrary_types_allowed = True
        json_encoders = {
            ComponentMeta: lambda x: None,
            models.Model: lambda x: x.pk,
            models.QuerySet: lambda qs: qs.values_list('pk', flat=True),
        }

    def __init_subclass__(cls, name=None, public=True, template_name=None):
        if public:
            name = name or cls.__name__
            cls._all[name] = cls
            cls._name = name

        if template_name is not None:
            cls._template_name = template_name

        for attr_name in vars(cls):
            attr = getattr(cls, attr_name)
            if (
                not attr_name.startswith('_')
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
    def _build(cls, _component_name, request, state):
        if _component_name not in cls._all:
            raise ComponentNotFound(
                f"Could not find requested component '{_component_name}'. "
                "Did you load the component?"
            )
        instance = cls._all[_component_name](**state)
        instance.__post_init__(request)
        return instance

    # Instance attributes
    id: str

    def __post_init__(self, request):
        self.__dict__['_meta'] = ComponentMeta(request)
        self.mounted()

    @property
    def user(self):
        return self._meta.user

    def destroy(self):
        self._meta.destroyed = True

    def redirect(self, url, **kwargs):
        self.redirect_raw_url(resolve_url(url, **kwargs))

    def redirect_raw_url(self, url):
        self._meta.headers["HX-Redirect"] = url

    def push_url(self, url, **kwargs):
        self.push_raw_url(resolve_url(url, **kwargs))

    def push_raw_url(self, url):
        self._meta.headers["HX-Push"] = url

    def _send_event(self, target, event):
        self._meta.triggers.after_swap(
            'hxSendEvent',
            {
                'target': target,
                'event': event,
            },
        )

    def _focus(self, selector):
        self._meta.triggers.after_settle('hxFocus', selector)

    def render(self):
        response = HttpResponse(self._render())
        for key, value in self._meta.all_headers.items():
            response[key] = value
        return response

    def mounted(self):
        """Called just after the component is instantiated and it `_meta` property
        have been set.

        Sub-classes SHOULD override this method to initialize the component.

        """
        pass

    def before_render(self) -> None:
        """Hook called before rendering the template.

        This allows to leave the `__init__` mostly empty, and push some
        computations after initialization just before rendering.  Which plays
        nicer with caching.

        This is your last chance to destroy the component if needed.

        """
        pass

    def _render(self, hx_swap_oob=False):
        with sentry_span(f"{self._fqn}._render"):
            with sentry_span(f"{self._fqn}.before_render"):
                self.before_render()
            if self._meta.destroyed:
                html = ''
            else:
                template = self._get_template()
                html = template.render(
                    self._get_context(hx_swap_oob),
                    request=self._meta.request,
                )
                html = html.strip()
            if self._meta.oob:
                html = '\n'.join(
                    chain(
                        [html],
                        [c._render(hx_swap_oob=True) for c in self._meta.oob],
                    )
                )
            return html

    def _also_render(self, component, **kwargs):
        instance = component(**kwargs)
        instance.__post_init__(self._meta.request)
        self._meta.oob.append(instance)

    def _get_template(self):
        if not self._meta.template:
            if isinstance(self._template_name, (list, tuple)):
                self._meta.template = select_template(self._template_name)
            else:
                self._meta.template = get_template(self._template_name)
        return self._meta.template

    def _get_context(self, hx_swap_oob):
        with sentry_span(f"{self._fqn}._get_context"):
            return dict(
                {
                    attr: getattr(self, attr)
                    for attr in dir(self)
                    if not attr.startswith('_')
                },
                this=self,
                hx_swap_oob=hx_swap_oob,
            )

    @property
    def _fqn(self) -> str:
        "Fully Qualified Name"
        cls = type(self)
        try:
            mod = cls.__module__
        except AttributeError:
            mod = ""
        name = cls.__name__
        return f"{mod}.{name}" if mod else name


class HTMXComponent(BaseHTMXComponent):
    pass


class CachedHTMXComponent(BaseHTMXComponent, CacheMixin, public=False):
    cache_name: t.ClassVar[str] = 'htmx'

    def _render(self, hx_swap_oob=False):
        return self._with_cache(super()._render, hx_swap_oob=hx_swap_oob)


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
