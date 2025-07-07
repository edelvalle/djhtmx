import datetime
import enum
import inspect
import operator
import types
from collections import defaultdict
from collections.abc import Callable, Iterable
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
from django.utils.datastructures import MultiValueDict
from pydantic import BeforeValidator, PlainSerializer, TypeAdapter

M = TypeVar("M", bound=models.Model)


@dataclass(slots=True)
class ModelRelatedField:
    name: str
    relation_name: str
    related_model_name: str


MODEL_RELATED_FIELDS: dict[type[models.Model], tuple[ModelRelatedField, ...]] = {}


class LazyModel:
    """Mark the model as lazy."""

    pass


@dataclass(slots=True, init=False)
class _LazyModelProxy(Generic[M]):  # noqa
    """Deferred proxy for a Django model instance; only fetches from the database on access."""

    _model: type[M]
    _instance: M | None
    _pk: Any | None

    def __init__(self, model: type[M], value: Any):
        self._model = model
        if value is None or isinstance(value, model):
            self._instance = value
            self._pk = getattr(value, "pk", None)
        else:
            self._instance = None
            self._pk = value

    def __getattr__(self, name: str) -> Any:
        if name == "pk":
            return self._pk
        if self._instance is None:
            self._instance = self._model.objects.get(pk=self._pk)
        return getattr(self._instance, name)

    def __repr__(self) -> str:
        return f"<_LazyModelProxy model={self._model}, pk={self._pk}, instance={self._instance}>"

    @classmethod
    def _get_before_validator(cls, model: type[M]):
        return BeforeValidator(_LazyModelBeforeValidator.from_modelclass(model))

    @classmethod
    def _get_plain_serializer(cls, model: type[M]):
        return PlainSerializer(
            func=_LazyModelPlainSerializer.from_modelclass(model),
            return_type=guess_pk_type(model),
        )

    @classmethod
    def _get_annotation(cls, model: type[M]):
        return Annotated[
            _LazyModelProxy[model],
            cls._get_before_validator(model),
            cls._get_plain_serializer(model),
        ]


@dataclass(slots=True)
class _LazyModelBeforeValidator(Generic[M]):  # noqa
    model: type[M]

    def __call__(self, value):
        if isinstance(value, _LazyModelProxy):
            instance = value._instance or value._pk
            return _LazyModelProxy(self.model, instance)
        else:
            return _LazyModelProxy(self.model, value)

    @classmethod
    @cache
    def from_modelclass(cls, model: type[M]):
        return cls(model)


@dataclass(slots=True)
class _LazyModelPlainSerializer(Generic[M]):  # noqa
    model: type[M]

    def __call__(self, value):
        if isinstance(value, _LazyModelProxy):
            return value._pk
        elif isinstance(value, self.model):
            return value.pk
        else:
            raise TypeError(f"Unexpected value {value}")

    @classmethod
    @cache
    def from_modelclass(cls, model: type[M]):
        return cls(model)


@dataclass(slots=True)
class _ModelBeforeValidator(Generic[M]):  # noqa
    model: type[M]

    def __call__(self, value):
        if value is None or isinstance(value, self.model):
            return value
        # If a component has a lazy model proxy, and passes it down to another component that
        # doesn't allow lazy proxies, we need to materialize it.
        elif isinstance(value, _LazyModelProxy):
            if value._pk is None:
                return None
            if value._instance is None:
                value._instance = value._model.objects.get(pk=value._pk)
            return value._instance
        else:
            return self.model.objects.get(pk=value)

    @classmethod
    @cache
    def from_modelclass(cls, model: type[M]):
        return cls(model)


@dataclass(slots=True)
class _ModelPlainSerializer(Generic[M]):  # noqa
    model: type[M]

    def __call__(self, value):
        return value.pk

    @classmethod
    @cache
    def from_modelclass(cls, model: type[M]):
        return cls(model)


def Model(model: type[models.Model]):
    assert issubclass_safe(model, models.Model)
    return Annotated[
        model,
        BeforeValidator(_ModelBeforeValidator.from_modelclass(model)),
        PlainSerializer(
            func=_ModelPlainSerializer.from_modelclass(model),
            return_type=guess_pk_type(model),
        ),
    ]


def QuerySet(qs: type[models.QuerySet]):
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


def annotate_model(annotation, *, _lazymodel: bool = False):
    if issubclass_safe(annotation, models.Model):
        if not _lazymodel:
            return Model(annotation)
        else:
            return _LazyModelProxy._get_annotation(annotation)
    elif issubclass_safe(annotation, models.QuerySet):
        return QuerySet(annotation)
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
                new_params = [p for p in params if not isinstance(p, LazyModel)]
                _lazymodel = set(new_params) != set(params)
                return type_[*(annotate_model(p, _lazymodel=_lazymodel) for p in params)]  # type: ignore
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
