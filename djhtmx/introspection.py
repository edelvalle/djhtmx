import inspect
import typing as t
from collections import defaultdict
from dataclasses import dataclass

from django.db import models
from django.utils.datastructures import MultiValueDict

# model


@dataclass(slots=True)
class ModelRelatedField:
    is_m2m: bool
    name: str
    relation_name: str
    related_model_name: str


MODEL_RELATED_FIELDS: dict[
    t.Type[models.Model], tuple[ModelRelatedField, ...]
] = {}


def get_related_fields(model):
    related_fields = MODEL_RELATED_FIELDS.get(model)
    if related_fields is None:
        fields = []
        for field in model._meta.get_fields():
            if (
                isinstance(field, (models.ForeignKey, models.ManyToManyField))
                and (relation_name := field.related_query_name())
                and relation_name != "+"
            ):
                is_m2m = isinstance(field, models.ManyToManyField)
                rel_meta = field.related_model._meta  # type: ignore
                fields.append(
                    ModelRelatedField(
                        is_m2m=is_m2m,
                        name=field.attname,
                        relation_name=relation_name,
                        related_model_name=(
                            f"{rel_meta.app_label}.{rel_meta.model_name}"
                        ),
                    )
                )
        related_fields = MODEL_RELATED_FIELDS[model] = tuple(fields)
    return related_fields


# filtering


def filter_parameters(f, kwargs):
    has_kwargs = any(
        param.kind == inspect.Parameter.VAR_KEYWORD
        for param in inspect.signature(f).parameters.values()
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


def parse_request_data(data: MultiValueDict[str, t.Any]):
    return _parse_obj(_extract_data(data))


def _extract_data(data: MultiValueDict[str, t.Any]):
    for key in set(data):
        if key.endswith("[]"):
            key = key.removesuffix("[]")
            value = data.getlist(key)
        else:
            value = data.get(key)
        yield key.split("."), value


def _parse_obj(
    data: t.Iterable[tuple[list[str], t.Any]], output=None
) -> dict[str, t.Any] | t.Any:
    output = output or {}
    arrays = defaultdict(lambda: defaultdict(dict))  # field -> index -> value
    for key, value in data:
        fragment, *tail = key
        if "[" in fragment:
            field_name = fragment[: fragment.index("[")]
            index = int(fragment[fragment.index("[") + 1 : -1])
            arrays[field_name][index] = (
                _parse_obj([(tail, value)], arrays[field_name][index])
                if tail
                else value
            )
        else:
            output[fragment] = _parse_obj([(tail, value)]) if tail else value

    for field, items in arrays.items():
        output[field] = [
            v for _, v in sorted(items.items(), key=lambda kv: kv[0])
        ]
    return output
