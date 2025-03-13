import dataclasses
import enum
import json
from collections.abc import Generator

import orjson
from django.core.serializers import deserialize, serialize
from django.core.serializers.base import DeserializedObject
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from pydantic import BaseModel

loads = orjson.loads


def dumps(obj):
    return orjson.dumps(obj, default).decode()


def encode(instance: models.Model) -> str:
    return serialize("json", [instance], cls=HtmxEncoder)


def decode(instance: str) -> models.Model:
    obj: DeserializedObject = next(iter(deserialize("json", instance)))
    obj.object.save = obj.save  # type: ignore
    return obj.object


class HtmxEncoder(json.JSONEncoder):
    def default(self, o):
        return default(o)


def default(o):
    try:
        return DjangoJSONEncoder().default(o)
    except TypeError:
        if hasattr(o, "__json__"):
            return o.__json__()

        if isinstance(o, models.Model):
            return o.pk

        if isinstance(o, Generator | set | frozenset):
            return list(o)

        if BaseModel and isinstance(o, BaseModel):
            return o.model_dump()

        if dataclasses.is_dataclass(o):
            return dataclasses.asdict(o)  # type: ignore

        if isinstance(o, enum.Enum):
            return o.value
        raise
