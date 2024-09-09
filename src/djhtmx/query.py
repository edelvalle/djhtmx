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

    The `ns` is the value of the component's attribute given by `ns_attr_name`
    (defaults to "hx_name_scrambled").

    If `auto_subscribe` is True (the default), the component is automatically
    subscribed to changes in the query string.  Otherwise, changes in the
    query string won't be signaled.

    .. important:: This attribute MUST be part of the state of the component.

    .. important:: If you're going to use non-shared parameters, you SHOULD
       really provide a consistent `ns_attr_name`.  Notice that if the
       `ns_attr_name` returns a shared value, you might get cross-components
       shares.

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

    def get_update_for_state(self, params: QueryDict, state: dict[str, t.Any]):
        if (param := params.get(self.param_name)) is not None and state.get(
            self.field_name
        ) != param:
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
