import json
import dataclasses
from typing import Generator

from pydantic import BaseModel

from django.core.serializers.json import DjangoJSONEncoder
from django.db import models


class HtmxEncoder(DjangoJSONEncoder):
    def default(self, o):
        if hasattr(o, '__json__'):
            return o.__json__()

        if isinstance(o, models.Model):
            return o.pk

        if isinstance(o, models.QuerySet):
            return list(o.values_list('pk', flat=True))

        if isinstance(o, (Generator, set)):
            return list(o)

        if BaseModel and isinstance(o, BaseModel):
            return o.dict()

        if dataclasses.is_dataclass(o):
            return dataclasses.asdict(o)

        return super().default(o)


loads = json.loads


def dumps(obj, cls=HtmxEncoder, *args, **kwargs):
    return json.dumps(obj, cls=cls, *args, **kwargs)
