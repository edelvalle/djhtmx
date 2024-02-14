import typing as t
from collections import defaultdict
from functools import cached_property
from itertools import chain
from pprint import pprint
from urllib.parse import urlparse
from uuid import uuid4

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AbstractUser, AnonymousUser
from django.db import models
from django.db.models.signals import post_save, pre_delete
from django.http import HttpRequest, HttpResponse, QueryDict
from django.shortcuts import resolve_url
from django.template import loader
from django.utils.html import format_html
from django.utils.safestring import SafeString, mark_safe
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    validate_call,
)

from . import json
from .introspection import annotate_model, get_related_fields
from .tracing import sentry_span


class ComponentNotFound(LookupError):
    pass


User = get_user_model()


RenderFunction = t.Callable[[dict[str, t.Any]], SafeString]


def get_params(request: HttpRequest) -> QueryDict:
    is_htmx_request = json.loads(request.META.get("HTTP_HX_REQUEST", "false"))
    if is_htmx_request:
        return QueryDict(
            urlparse(request.META["HTTP_HX_CURRENT_URL"]).query,
            mutable=True,
        )
    else:
        return request.GET.copy()


class Repository:
    def __init__(
        self,
        request: HttpRequest,
        state_by_id: dict[str, dict[str, t.Any]] = None,
        subscriptions_by_id: dict[str, list[str]] = None,
    ):
        self.request = request
        self.component_by_id: dict[str, "PydanticComponent"] = {}
        self.state_by_id = state_by_id or {}
        self.subscriptions_by_id = subscriptions_by_id or {}

        self.params = get_params(request)
        self.signals = set()

        if self.subscriptions_by_id:
            post_save.connect(
                receiver=self._listen_to_post_save,
            )
            pre_delete.connect(
                receiver=self._listen_to_pre_delete,
            )

    def _listen_to_post_save(
        self,
        sender: t.Type[models.Model],
        instance: models.Model,
        created: bool,
        **kwargs,
    ):
        app = sender._meta.app_label
        name = sender._meta.model_name
        self.signals.update([f"{app}.{name}", f"{app}.{name}.{instance.pk}"])
        action = "created" if created else "updated"
        self.signals.add(f"{app}.{name}.{instance.pk}.{action}")
        self._listen_to_realted(sender, instance, action=action)

    def _listen_to_pre_delete(
        self,
        sender: t.Type[models.Model],
        instance: models.Model,
        **kwargs,
    ):
        app = sender._meta.app_label
        name = sender._meta.model_name
        self.signals.update(
            [
                f"{app}.{name}",
                f"{app}.{name}.{instance.pk}",
                f"{app}.{name}.{instance.pk}.deleted",
            ]
        )
        self._listen_to_realted(sender, instance, action="deleted")

    def _listen_to_realted(
        self,
        sender: t.Type[models.Model],
        instance: models.Model,
        action: str,
    ):
        for field in get_related_fields(sender):
            fk_id = getattr(instance, field.name)
            signal = f"{field.related_model_name}.{fk_id}.{field.relation_name}"
            self.signals.update((signal, f"{signal}.{action}"))

    def dispatch_signals(self):
        if settings.DEBUG and self.signals:
            print("LAUNCHED SIGNALS:")
            pprint(self.signals)

        for component_id, subscriptions in self.subscriptions_by_id.items():
            if (
                self.signals.intersection(subscriptions)
                and (state := self.state_by_id.pop(component_id, None))
                is not None
            ):
                component = self.register_component(
                    build(state["hx_name"], self.request, self.params, state)
                )
                if settings.DEBUG:
                    print("> MATCHED: ", state["hx_name"], subscriptions)
                yield self.render_html(component, oob="true")

    def render_oob(self):
        for component in self.component_by_id.values():
            for oob, component in component.controller._oob:
                yield self.render_html(component, oob=oob)

    def build(self, component_name: str, state: dict[str, t.Any]):
        if component_id := state.get("id"):
            if component := self.component_by_id.get(component_id):
                for key, value in state.items():
                    setattr(component, key, value)
                return component
            elif stored_state := self.state_by_id.pop(component_id, None):
                state = stored_state | state

        component = build(component_name, self.request, self.params, state)
        return self.register_component(component)

    def register_component(
        self,
        component: "PydanticComponent",
    ) -> "PydanticComponent":
        self.component_by_id[component.id] = component
        return component

    def render(
        self, component: "PydanticComponent", template: str | None = None
    ):
        return component.controller.render(
            component._get_template(template),
            component._get_context() | {"htmx_repo": self},
        )

    def render_html(
        self,
        component: "PydanticComponent",
        oob: str = None,
    ):
        is_oob = oob not in ("true", None)
        html = [
            format_html('<div hx-swap-oob="{oob}">', oob=oob)
            if is_oob
            else None,
            component.controller.render_html(
                component._get_template(),
                component._get_context()
                | {"htmx_repo": self, "hx_oob": None if is_oob else oob},
            ),
            "</div>" if is_oob else None,
        ]
        return mark_safe("".join(filter(None, html)))


class Controller:
    def __init__(self, request: HttpRequest, params: QueryDict):
        self.request = request
        self.params = params
        self._destroyed: bool = False
        self._headers: dict[str, str] = {}
        self._oob: list[tuple[str, "PydanticComponent"]] = []

    def build(self, component: t.Type["PydanticComponent"], **state):
        clone = type(self)(self.request, self.params)
        return component(controller=clone, hx_name=component.__name__, **state)

    def destroy(self):
        self._destroyed = True

    def append(self, target: str, component: "PydanticComponent"):
        self._oob.append((f"beforeend:{target}", component))

    def prepend(self, target: str, component: "PydanticComponent"):
        self._oob.append((f"afterbegin:{target}", component))

    def after(self, target: str, component: "PydanticComponent"):
        self._oob.append((f"afterend:{target}", component))

    def before(self, target: str, component: "PydanticComponent"):
        self._oob.append((f"beforebegin:{target}", component))

    def update(self, component: "PydanticComponent"):
        self._oob.append(("true", component))

    @cached_property
    def triggers(self):
        return Triggers()

    def redirect_to(self, url, **kwargs):
        self._headers["HX-Redirect"] = resolve_url(url, **kwargs)

    def focus(self, selector):
        self.triggers.after_settle("hxFocus", selector)

    def dispatch_event(self, target: str, event: str):
        self.triggers.after_settle(
            "hxDispatchEvent",
            {
                "target": target,
                "event": event,
            },
        )

    def render(self, render: RenderFunction, context: dict[str, t.Any]):
        response = HttpResponse(self.render_html(render, context))
        for key, value in (self._headers | self.triggers.headers).items():
            response[key] = value
        return response

    def render_html(self, render: RenderFunction, context: dict[str, t.Any]):
        if self._destroyed:
            html = ""
        else:
            html = render(context | {"request": self.request}).strip()
        return mark_safe(html)


REGISTRY: dict[str, t.Type["PydanticComponent"]] = {}
FQN: dict[t.Type["PydanticComponent"], str] = {}
RENDER_FUNC: dict[str, RenderFunction] = {}


A = t.TypeVar("A")
B = t.TypeVar("B")
P = t.ParamSpec("P")


def _compose(f: t.Callable[P, A], g: t.Callable[[A], B]) -> t.Callable[P, B]:
    def result(*args: P.args, **kwargs: P.kwargs):
        return g(f(*args, **kwargs))

    return result


def get_template(template: str) -> t.Callable[..., SafeString]:
    if settings.DEBUG:
        return _compose(loader.get_template(template).render, mark_safe)
    else:
        if (render := RENDER_FUNC.get(template)) is None:
            render = _compose(loader.get_template(template).render, mark_safe)
            RENDER_FUNC[template] = render
        return render


def build(
    component_name: str,
    request: HttpRequest,
    params: QueryDict,
    state: dict[str, t.Any],
):
    if component_name not in REGISTRY:
        raise ComponentNotFound(
            f"Could not find requested component '{component_name}'. "
            "Did you load the component?"
        )

    return REGISTRY[component_name](
        **dict(
            state,
            hx_name=component_name,
            controller=Controller(request, params),
        )
    )


class PydanticComponent(BaseModel):
    _template_name: str = ...  # type: ignore

    # fields to exclude from component state during serialization
    model_config = ConfigDict(
        arbitrary_types_allowed=True,
    )

    def __init_subclass__(cls, public=True):
        FQN[cls] = f"{cls.__module__}.{cls.__name__}"

        if public:
            REGISTRY[cls.__name__] = cls

        for name, annotation in list(cls.__annotations__.items()):
            if not name.startswith("_"):
                cls.__annotations__[name] = annotate_model(annotation)

        for attr_name in vars(cls):
            attr = getattr(cls, attr_name)
            if (
                not (
                    attr_name.startswith("_") or attr_name.startswith("model_")
                )
                and attr_name.islower()
                and callable(attr)
            ):
                setattr(
                    cls,
                    attr_name,
                    validate_call(
                        config={"arbitrary_types_allowed": True}
                    )(attr),
                )

        return super().__init_subclass__()

    # State
    id: str = Field(default_factory=lambda: f"hx-{uuid4().hex}")
    controller: Controller = Field(exclude=True)

    hx_name: str

    @classmethod
    def _build(cls, controller: Controller, **state):
        return cls(controller=controller, hx_name=cls.__name__, **state)

    @property
    def subscriptions(self) -> set[str]:
        return set()

    @cached_property
    def user(self) -> User | AnonymousUser:
        user = getattr(self.controller.request, "user", None)
        if user is None or not isinstance(user, User):
            return AnonymousUser()
        return user

    def _get_template(
        self, template: str | None = None
    ) -> t.Callable[..., SafeString]:
        return get_template(template or self._template_name)

    def _get_context(self):
        with sentry_span(f"{FQN[type(self)]}._get_context"):
            return {
                attr: getattr(self, attr)
                for attr in dir(self)
                if not attr.startswith("_") and not attr.startswith("model_")
            } | {"this": self}


class Triggers:
    def __init__(self):
        self._trigger = defaultdict(list)
        self._after_swap = defaultdict(list)
        self._after_settle = defaultdict(list)

    def add(self, name, what: t.Any):
        self._trigger[name].append(what)

    def after_swap(self, name, what: t.Any):
        self._after_swap[name].append(what)

    def after_settle(self, name, what: t.Any):
        self._after_settle[name].append(what)

    @property
    def headers(self):
        headers = [
            ("HX-Trigger", self._trigger),
            ("HX-Trigger-After-Swap", self._after_swap),
            ("HX-Trigger-After-Settle", self._after_settle),
        ]
        return {header: json.dumps(value) for header, value in headers if value}


# Legacy Components


class Component:
    template_name = ""
    template = None
    _all = {}
    _urls = {}
    _name = ...

    _pydantic_config = ConfigDict(
        {
            "arbitrary_types_allowed": True,
        }
    )

    def __init_subclass__(cls, name=None, public=True):
        if public:
            name = name or cls.__name__
            cls._all[name] = cls
            cls._name = name

        for attr_name in vars(cls):
            attr = getattr(cls, attr_name)
            if (
                attr_name == "__init__"
                or not attr_name.startswith("_")
                and attr_name.islower()
                and callable(attr)
            ):
                setattr(
                    cls,
                    attr_name,
                    validate_call(config=cls._pydantic_config)(attr),  # type: ignore
                )

        return super().__init_subclass__()

    @classmethod
    def _build(cls, _component_name, request, id, state):
        if _component_name not in cls._all:
            raise ComponentNotFound(
                f"Could not find requested component '{_component_name}'. Did you load the component?"
            )
        return cls._all[_component_name](**dict(state, id=id, request=request))

    def __init__(self, request: HttpRequest, id: str | None = None):
        self.request = request
        self.id = id
        self._destroyed = False
        self._headers = {}
        self._triggers = Triggers()
        self._oob = []

    @cached_property
    def user(self) -> AbstractUser | AnonymousUser:
        user = getattr(self.request, "user", None)
        if user is None or not isinstance(user, AbstractUser):
            return AnonymousUser()
        return user

    @property
    def _state_json(self) -> str:
        return json.dumps(self._state)

    @property
    def _state(self) -> dict:
        if schema := getattr(self.__init__, "__pydantic_core_schema__", None):
            # This is Pydantic v2 which doesn't expose a model, but creates a
            # direct validator.
            call_args = schema["arguments_schema"]  # of type 'call'
            fn_args = call_args["arguments_schema"]
            return {
                name: getattr(self, name)
                for arg in fn_args
                if (name := arg["name"]) != "self"
                if hasattr(self, name)
            }
        elif model := getattr(self.__init__, "model", None):
            return {
                name: getattr(self, name)
                for name in model.__fields__
                if hasattr(self, name)
            }
        else:
            return {}

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
            "hxSendEvent",
            {
                "target": target,
                "event": event,
            },
        )

    def _focus(self, selector):
        self._triggers.after_settle("hxFocus", selector)

    def render(self, template: str | None = None):
        response = HttpResponse(self._render(template=template))
        for key, value in (self._headers | self._triggers.headers).items():
            response[key] = value
        return response

    def before_render(self) -> None:
        """Hook called before rendering the template.

        This allows to leave the `__init__` mostly empty, and push some
        computations after initialization just before rendering.  Which plays
        nicer with caching.

        This is your last chance to destroy the component if needed.

        """
        pass

    def _render(self, hx_swap_oob=False, template: str | None = None):
        with sentry_span(f"{self._fqn}._render"):
            with sentry_span(f"{self._fqn}.before_render"):
                self.before_render()
            if self._destroyed:
                html = ""
            else:
                html = mark_safe(
                    self._get_template(template)
                    .render(
                        self._get_context(hx_swap_oob),
                        request=self.request,
                    )
                    .strip()
                )
            if self._oob:
                html = mark_safe(
                    "\n".join(
                        chain(
                            [html],
                            [c._render(hx_swap_oob=True) for c in self._oob],
                        )
                    )
                )
            return html

    def _also_render(self, component, **kwargs):
        self._oob.append(component(request=self.request, **kwargs))

    def _get_template(self, template: str | None = None):
        if template:
            return loader.get_template(template)
        elif not self.template:
            self.template = loader.get_template(self.template_name)
        return self.template

    def _get_context(self, hx_swap_oob):
        with sentry_span(f"{self._fqn}._get_context"):
            return dict(
                {
                    attr: getattr(self, attr)
                    for attr in dir(self)
                    if not attr.startswith("_")
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
