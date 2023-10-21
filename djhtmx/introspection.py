import typing as t
from collections import defaultdict
from django.utils.datastructures import MultiValueDict

import inspect


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
            if param in f.model.__fields__
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
