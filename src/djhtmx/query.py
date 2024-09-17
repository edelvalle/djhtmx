from __future__ import annotations

import datetime
import re
import types
import typing as t
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from django.http import QueryDict
from pydantic import BaseModel
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

from djhtmx.utils import compact_hash


@dataclass(slots=True)
class Query:
    """Annotation to integrate the state with the URL's query string.

    By default the query string name can be shared across many components,
    provided the have the same type annotation.

    You can set `shared` to False, to make this a specific (by component id)
    param.  In this case the URL is `<name>__<ns>=value`.

    """

    name: str
    shared: bool = True

    def __post_init__(self):
        assert _VALID_QS_NAME_RX.match(self.name) is not None, self.name

    @classmethod
    def extract_from_field_info(cls, name: str, field: FieldInfo):
        done = False
        for meta in field.metadata:
            if isinstance(meta, cls):
                if done:
                    raise TypeError(
                        f"Field '{name}' in component {cls.__qualname__} "
                        " has more than one Query annotation."
                    )
                if not (
                    field.default is not PydanticUndefined or field.default_factory is not None
                ):
                    raise TypeError(
                        f"Field '{name}' of {cls.__qualname__} must have "
                        "a default or default_factory."
                    )

                yield meta
                done = True


@dataclass(slots=True)
class QueryPatcher:
    field_name: str
    param_name: str
    signal_name: str

    @classmethod
    def for_component(cls, component: type[BaseModel]):
        seen = set()
        for field_name, field in component.model_fields.items():
            for query in Query.extract_from_field_info(field_name, field):
                name = query.name
                if name in seen:
                    raise TypeError(
                        f"Component {component.__name__} has multiple "
                        f"fields with the same query param '{name}'"
                    )
                seen.add(name)

                # Convert parameter from `search_query` to `search-query`
                param_name = name.replace("_", "-")

                # Prefix with the component name if not shared
                if not query.shared:
                    param_name = f"{param_name}-{compact_hash(component.__name__)}"

                yield cls(
                    field_name=field_name,
                    param_name=param_name,
                    signal_name=f"querystring.{param_name}",
                )

    def get_update_for_state(self, params: QueryDict):
        if (param := params.get(self.param_name)) is not None:
            return {self.field_name: param}
        else:
            return {}

    def get_updates_for_params(self, value: str | None, params: QueryDict) -> list[str]:
        if value == params.get(self.param_name):
            return []
        else:
            params[self.param_name] = str(value)
            return [self.signal_name]


_VALID_QS_NAME_RX = re.compile(r"^[a-zA-Z_\d][-a-zA-Z_\d]*$")
_SIMPLE_TYPES = (int, str, float, UUID, types.NoneType, date, datetime, bool)
