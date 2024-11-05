import datetime
import enum
import inspect
import types
import typing as t
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from functools import cache
from inspect import Parameter, _ParameterKind
from uuid import UUID

from django.apps import apps
from django.db import models
from django.utils.datastructures import MultiValueDict
from pydantic import BeforeValidator, PlainSerializer, TypeAdapter

# model


@dataclass(slots=True)
class ModelRelatedField:
    name: str
    relation_name: str
    related_model_name: str


MODEL_RELATED_FIELDS: dict[type[models.Model], tuple[ModelRelatedField, ...]] = {}


def Model(model: type[models.Model]):
    return t.Annotated[
        model,
        BeforeValidator(
            lambda value: (
                value
                if value is None or isinstance(value, model)
                else model.objects.filter(pk=value).first()
            )
        ),
        PlainSerializer(
            func=lambda v: v.pk,
            return_type=str if (pk := model().pk) is None else type(pk),
        ),
    ]


def QuerySet(qs: type[models.QuerySet]):
    [model] = [m for m in apps.get_models() if isinstance(m.objects.all(), qs)]
    return t.Annotated[
        qs,
        BeforeValidator(lambda v: (v if isinstance(v, qs) else model.objects.filter(pk__in=v))),
        PlainSerializer(
            func=lambda v: (
                [instance.pk for instance in v]
                if v._result_cache
                else list(v.values_list("pk", flat=True))
            ),
            return_type=str if (pk := model().pk) is None else type(pk),
        ),
    ]


def annotate_model(annotation):
    if issubclass_safe(annotation, models.Model):
        return Model(annotation)
    elif issubclass_safe(annotation, models.QuerySet):
        return QuerySet(annotation)
    elif t.is_typeddict(annotation):
        return t.TypedDict(
            annotation.__name__,  # type: ignore
            {
                k: annotate_model(v)  # type: ignore
                for k, v in t.get_type_hints(annotation).items()
            },
        )
    elif type_ := t.get_origin(annotation):
        if type_ is types.UnionType or type_ is t.Union:
            type_ = t.Union
        match t.get_args(annotation):
            case ():
                return type_
            case (param,):
                return type_[annotate_model(param)]  # type: ignore
            case params:
                return type_[*(annotate_model(p) for p in params)]  # type: ignore
    else:
        return annotation


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


@cache
def get_function_parameters(
    function: t.Callable,
    exclude_kinds: tuple[_ParameterKind, ...] = (),
) -> set[str]:
    return set(
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


def filter_parameters(f: t.Callable, kwargs: dict[str, t.Any]):
    has_kwargs = any(
        param.kind == Parameter.VAR_KEYWORD for param in inspect.signature(f).parameters.values()
    )
    if has_kwargs:
        return kwargs
    else:
        return {
            param: value
            for param, value in kwargs.items()
            if param in inspect.signature(f).parameters.keys()
        }


# Decoder for client requests


def parse_request_data(data: MultiValueDict[str, t.Any] | dict[str, t.Any]):
    if not isinstance(data, MultiValueDict):
        data = MultiValueDict({
            key: value if isinstance(value, list) else [value] for key, value in data.items()
        })
    return _parse_obj(_extract_data(data))


def _extract_data(data: MultiValueDict[str, t.Any]):
    for key in set(data):
        if key.endswith("[]"):
            value = data.getlist(key)
            key = key.removesuffix("[]")
        else:
            value = data.get(key)
        yield key.split("."), value


def _parse_obj(data: t.Iterable[tuple[list[str], t.Any]], output=None) -> dict[str, t.Any] | t.Any:
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
        output[field] = [v for _, v in sorted(items.items(), key=lambda kv: kv[0])]
    return output


def get_event_handler_event_types(f: t.Callable[..., t.Any]) -> set[type]:
    "Extract the types of the annotations of parameter 'event'."
    event = t.get_type_hints(f)["event"]
    if t.get_origin(event) is types.UnionType:
        return {
            arg for arg in t.get_args(event) if isinstance(arg, type) and arg is not types.NoneType
        }
    elif isinstance(event, type):
        return {event}
    else:
        return set()


def get_annotation_adapter(annotation):
    """Return a TypeAdapter for the annotation."""
    if annotation is bool:
        return infallible_bool_adapter

    return TypeAdapter(
        t.Optional[annotate_model(annotation)]  # type: ignore
    )


# Infallible adapter for boolean values.  't' is True, everything else is
# False.
infallible_bool_adapter = TypeAdapter(
    t.Annotated[
        bool,
        BeforeValidator(lambda v: True if v == "t" else False),
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

    """
    return (
        ann in _SIMPLE_TYPES
        or issubclass_safe(ann, models.Model)
        or issubclass_safe(ann, (enum.IntEnum, enum.StrEnum))
    )


def is_union_of_basic(ann):
    """Returns True Union of simple types (as is_simple_annotation)"""
    type_ = t.get_origin(ann)
    if type_ is types.UnionType or type_ is t.Union:
        return all(is_basic_type(arg) for arg in t.get_args(ann))
    return False


def is_simple_annotation(ann):
    "Return True if the annotation is either simple or a Union of simple"
    return is_basic_type(ann) or is_union_of_basic(ann)


Unset = object()
_SIMPLE_TYPES = (int, str, float, UUID, types.NoneType, date, datetime, bool)
