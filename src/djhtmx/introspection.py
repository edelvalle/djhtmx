import datetime
import enum
import inspect
import operator
import types
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from functools import cache
from inspect import Parameter, _ParameterKind
from typing import (
    Annotated,
    Any,
    Generic,
    TypedDict,
    TypeVar,
    Union,
    get_args,
    get_origin,
    get_type_hints,
    is_typeddict,
)
from uuid import UUID

from django.apps import apps
from django.db import models
from django.db.models import Prefetch
from django.utils.datastructures import MultiValueDict
from pydantic import BeforeValidator, PlainSerializer, TypeAdapter

M = TypeVar("M", bound=models.Model)


@dataclass(slots=True)
class ModelRelatedField:
    name: str
    relation_name: str
    related_model_name: str


MODEL_RELATED_FIELDS: dict[type[models.Model], tuple[ModelRelatedField, ...]] = {}


@dataclass(slots=True, unsafe_hash=True)
class ModelConfig:
    """Annotation to configure fetching the models/querysets in pydantic models.

    For this configuration to take place the pydantic model has to call `annotate_model`.  HTMX
    components require no extra steps.

    """

    lazy: bool = False
    """If set to True, annotations of models.Model will return a _LazyModelProxy instead of the
       actual model instance.

    """

    select_related: list[str] | tuple[str, ...] | None = None
    """The arguments to `model.objects.select_related(*select_related)`."""

    prefetch_related: list[str | Prefetch] | tuple[str | Prefetch, ...] | None = None
    """The arguments to `model.objects.prefetch_related(*prefetch_related)`."""


_DEFAULT_MODEL_CONFIG = ModelConfig()


@dataclass(slots=True, init=False)
class _LazyModelProxy(Generic[M]):  # noqa
    """Deferred proxy for a Django model instance; only fetches from the database on access."""

    __model: type[M]
    __instance: M | None
    __pk: Any | None
    __select_related: Sequence[str] | None
    __prefetch_related: Sequence[str | Prefetch] | None

    def __init__(
        self,
        model: type[M],
        value: Any,
        model_annotation: ModelConfig | None = None,
    ):
        self.__model = model
        if value is None or isinstance(value, model):
            self.__instance = value
            self.__pk = getattr(value, "pk", None)
        else:
            self.__instance = None
            self.__pk = value
        if model_annotation:
            self.__select_related = model_annotation.select_related
            self.__prefetch_related = model_annotation.prefetch_related
        else:
            self.__select_related = None
            self.__prefetch_related = None

    def __getattr__(self, name: str) -> Any:
        if name == "pk":
            return self.__pk
        if self.__instance is None:
            self.__ensure_instance()
        return getattr(self.__instance, name)

    def __ensure_instance(self):
        if not self.__instance:
            manager = self.__model.objects
            if select_related := self.__select_related:
                manager = manager.select_related(*select_related)
            if prefetch_related := self.__prefetch_related:
                manager = manager.prefetch_related(*prefetch_related)
            self.__instance = manager.get(pk=self.__pk)
        return self.__instance

    def __repr__(self) -> str:
        return f"<_LazyModelProxy model={self.__model}, pk={self.__pk}, instance={self.__instance}>"


@dataclass(slots=True)
class _ModelBeforeValidator(Generic[M]):  # noqa
    model: type[M]
    model_config: ModelConfig

    def __call__(self, value):
        if self.model_config.lazy:
            return self._get_lazy_proxy(value)
        else:
            return self._get_instance(value)

    def _get_lazy_proxy(self, value):
        if isinstance(value, _LazyModelProxy):
            instance = value._LazyModelProxy__instance or value._LazyModelProxy__pk
            return _LazyModelProxy(self.model, instance)
        else:
            return _LazyModelProxy(self.model, value)

    def _get_instance(self, value):
        if value is None or isinstance(value, self.model):
            return value
        # If a component has a lazy model proxy, and passes it down to another component that
        # doesn't allow lazy proxies, we need to materialize it.
        elif isinstance(value, _LazyModelProxy):
            return value._LazyModelProxy__ensure_instance()
        else:
            manager = self.model.objects
            if select_related := self.model_config.select_related:
                manager = manager.select_related(*select_related)
            if prefetch_related := self.model_config.prefetch_related:
                manager = manager.prefetch_related(*prefetch_related)
            return manager.get(pk=value)

    @classmethod
    @cache
    def from_modelclass(cls, model: type[M], model_config: ModelConfig):
        return cls(model, model_config=model_config)


@dataclass(slots=True)
class _ModelPlainSerializer(Generic[M]):  # noqa
    model: type[M]

    def __call__(self, value):
        return value.pk

    @classmethod
    @cache
    def from_modelclass(cls, model: type[M]):
        return cls(model)


def _Model(model: type[models.Model], model_config: ModelConfig | None = None):
    assert issubclass_safe(model, models.Model)
    model_config = model_config or _DEFAULT_MODEL_CONFIG
    return Annotated[
        model if not model_config.lazy else _LazyModelProxy[model],
        BeforeValidator(_ModelBeforeValidator.from_modelclass(model, model_config)),
        PlainSerializer(
            func=_ModelPlainSerializer.from_modelclass(model),
            return_type=guess_pk_type(model),
        ),
    ]


def _QuerySet(qs: type[models.QuerySet]):
    [model] = [m for m in apps.get_models() if isinstance(m.objects.all(), qs)]
    return Annotated[
        qs,
        BeforeValidator(lambda v: (v if isinstance(v, qs) else model.objects.filter(pk__in=v))),
        PlainSerializer(
            func=lambda v: (
                [instance.pk for instance in v]
                if v._result_cache
                else list(v.values_list("pk", flat=True))
            ),
            return_type=guess_pk_type(model),
        ),
    ]


def annotate_model(annotation, *, model_config: ModelConfig | None = None):
    if issubclass_safe(annotation, models.Model):
        return _Model(annotation, model_config)
    elif issubclass_safe(annotation, models.QuerySet):
        return _QuerySet(annotation)
    elif is_typeddict(annotation):
        return TypedDict(
            annotation.__name__,  # type: ignore
            {
                k: annotate_model(v)  # type: ignore
                for k, v in get_type_hints(annotation).items()
            },
        )
    elif type_ := get_origin(annotation):
        if type_ is types.UnionType or type_ is Union:
            type_ = Union
        match get_args(annotation):
            case ():
                return type_
            case (param,):
                return type_[annotate_model(param)]  # type: ignore
            case params:
                model_annotation = next(
                    (p for p in params if isinstance(p, ModelConfig)),
                    None,
                )
                return type_[*(annotate_model(p, model_config=model_annotation) for p in params)]  # type: ignore
    else:
        return annotation


def guess_pk_type(model: type[models.Model]):
    match model._meta.pk:
        case models.UUIDField():
            return UUID
        case models.IntegerField():
            return int
        case _:
            return str


def isinstance_safe(o, types):
    try:
        return isinstance(o, types)
    except TypeError:
        return False


def issubclass_safe(o, types):
    try:
        return issubclass(o, types)
    except TypeError:
        return False


# for state of old components


def get_function_parameters(
    function: Callable,
    exclude_kinds: tuple[_ParameterKind, ...] = (),
) -> frozenset[str]:
    return frozenset(
        param.name
        for param in inspect.signature(function).parameters.values()
        if param.name != "self" and param.kind not in exclude_kinds
    )


@cache
def get_related_fields(model):
    related_fields = MODEL_RELATED_FIELDS.get(model)
    if related_fields is None:
        fields = []
        for field in model._meta.get_fields():
            if (
                isinstance(field, models.ForeignKey)
                and (relation_name := field.related_query_name())
                and relation_name != "+"
            ):
                rel_meta = field.related_model._meta  # type: ignore
                fields.append(
                    ModelRelatedField(
                        name=field.attname,
                        relation_name=relation_name,
                        related_model_name=(f"{rel_meta.app_label}.{rel_meta.model_name}"),
                    )
                )
        related_fields = MODEL_RELATED_FIELDS[model] = tuple(fields)
    return related_fields


# filtering


def filter_parameters(f: Callable, kwargs: dict[str, Any]):
    has_kwargs = any(
        param.kind == Parameter.VAR_KEYWORD for param in inspect.signature(f).parameters.values()
    )
    if has_kwargs:
        return kwargs
    else:
        return {
            param: value
            for param, value in kwargs.items()
            if param in inspect.signature(f).parameters
        }


# Decoder for client requests


def parse_request_data(data: MultiValueDict[str, Any] | dict[str, Any]):
    if not isinstance(data, MultiValueDict):
        data = MultiValueDict({
            key: value if isinstance(value, list) else [value] for key, value in data.items()
        })
    return _parse_obj(_extract_data(data))


def _extract_data(data: MultiValueDict[str, Any]):
    for key in set(data):
        if key.endswith("[]"):
            value = data.getlist(key)
            key = key.removesuffix("[]")
        else:
            value = data.get(key)
        yield key.split("."), value


def _parse_obj(data: Iterable[tuple[list[str], Any]], output=None) -> dict[str, Any] | Any:
    output = output or {}
    arrays = defaultdict(lambda: defaultdict(dict))  # field -> index -> value
    for key, value in data:
        fragment, *tail = key
        if "[" in fragment:
            field_name = fragment[: fragment.index("[")]
            index = int(fragment[fragment.index("[") + 1 : -1])
            arrays[field_name][index] = (
                _parse_obj([(tail, value)], arrays[field_name][index]) if tail else value
            )
        else:
            output[fragment] = _parse_obj([(tail, value)]) if tail else value

    for field, items in arrays.items():
        output[field] = [v for _, v in sorted(items.items(), key=operator.itemgetter(0))]
    return output


def get_event_handler_event_types(f: Callable[..., Any]) -> set[type]:
    "Extract the types of the annotations of parameter 'event'."
    event = get_type_hints(f)["event"]
    origin = get_origin(event)
    if origin is types.UnionType or origin is Union:
        return {
            arg for arg in get_args(event) if isinstance(arg, type) and arg is not types.NoneType
        }
    elif isinstance(event, type):
        return {event}
    else:
        return set()


def get_annotation_adapter(annotation):
    """Return a TypeAdapter for the annotation."""
    if annotation is bool:
        return infallible_bool_adapter

    return TypeAdapter(annotation, config={"arbitrary_types_allowed": True})


# Infallible adapter for boolean values.  't' is True, everything else is
# False.
infallible_bool_adapter = TypeAdapter(
    Annotated[
        bool,
        BeforeValidator(lambda v: v == "t"),
        PlainSerializer(lambda v: "t" if v else "f"),
    ]
)


def is_basic_type(ann):
    """Returns True if the annotation is a simple type.

    Simple types are:

    - Simple Python types: ints, floats, strings, UUIDs, dates and datetimes, bools,
      and the value None.

    - Instances of a Django model (which will use the PK as a proxy)

    - Instances of IntEnum or StrEnum.

    - Instances of dict, tuple, list or set

    """
    return (
        ann in _SIMPLE_TYPES
        #  __origin__ -> model in 'Annotated[model, BeforeValidator(...), PlainSerializer(...)]'
        or issubclass_safe(getattr(ann, "__origin__", None), models.Model)
        or issubclass_safe(ann, (enum.IntEnum, enum.StrEnum))
        or is_collection_annotation(ann)
    )


def is_union_of_basic(ann):
    """Returns True Union of simple types (as is_simple_annotation)"""
    type_ = get_origin(ann)
    if type_ is types.UnionType or type_ is Union:
        return all(is_basic_type(arg) for arg in get_args(ann))
    return False


def is_simple_annotation(ann):
    "Return True if the annotation is either simple or a Union of simple"
    return is_basic_type(ann) or is_union_of_basic(ann)


def is_collection_annotation(ann):
    return issubclass_safe(ann, _COLLECTION_TYPES)


Unset = object()
_SIMPLE_TYPES = (int, str, float, UUID, types.NoneType, date, datetime, bool)
_COLLECTION_TYPES = (dict, tuple, list, set)
