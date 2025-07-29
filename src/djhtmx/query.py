from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from django.http import QueryDict
from pydantic import BaseModel, TypeAdapter
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

from djhtmx.introspection import (
    get_annotation_adapter,
    is_collection_annotation,
    is_simple_annotation,
)
from djhtmx.utils import compact_hash


@dataclass(slots=True, unsafe_hash=True)
class Query:
    """Annotation to integrate the state with the URL's query string.

    By default the query string name can be shared across many components,
    provided the have the same type annotation.

    You can set `shared` to False, to make this a specific (by component id)
    param.  In this case the URL is `<name>__<ns>=value`.

    If `auto_subscribe` is True (the default), the component is automatically
    subscribed to changes in the query string.  Otherwise, changes in the
    query string won't be signaled.

    """

    name: str
    shared: bool = True
    auto_subscribe: bool = True

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
    auto_subscribe: bool

    default_value: Any
    adapter: TypeAdapter[Any]

    use_json: bool

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

                # Check the type annotation.  It must be something that can
                # reasonably be put in the URL: basic types or union of basic
                # types.
                annotation = field.annotation
                if not is_simple_annotation(annotation):
                    raise TypeError(f"Invalid type annotation {annotation} for a query string")

                # The field must have a default to be Query.
                if field.default is PydanticUndefined and field.default_factory is None:
                    raise TypeError(
                        f"Field '{name}' of {component.__name__} must have "
                        "a default or default_factory."
                    )

                # Convert parameter from `search_query` to `search-query`
                param_name = name.replace("_", "-")

                # Prefix with the component name if not shared
                if not query.shared:
                    param_name = f"{param_name}-{compact_hash(component.__name__)}"
                adapter = get_annotation_adapter(field.annotation)
                yield cls(
                    field_name=field_name,
                    param_name=param_name,
                    signal_name=f"querystring.{param_name}",
                    auto_subscribe=query.shared and query.auto_subscribe,
                    default_value=field.get_default(call_default_factory=True),
                    adapter=adapter,
                    use_json=is_collection_annotation(annotation),
                )

    def get_update_for_state(self, params: QueryDict):
        if (raw_param := params.get(self.param_name)) is not None:
            # We need to perform the validation during patching, otherwise
            # ill-formed values in the query will cause a Pydantic
            # ValidationError, but we should just simply ignore invalid
            # values.
            try:
                return {
                    self.field_name: self.adapter.validate_json(raw_param)
                    if self.use_json
                    else self.adapter.validate_python(raw_param)
                }
            except ValueError:
                # Preserve the last good known state in the component
                return {}
        else:
            return {self.field_name: self.default_value}

    def get_updates_for_params(self, value: Any, params: QueryDict) -> list[str]:
        # If we're setting the default value, let remove it from the query
        # string completely, and trigger the signal if needed.
        if value == self.default_value:
            if self.param_name in params:
                params.pop(self.param_name, None)
                return [self.signal_name]
            else:
                return []

        # Otherwise, let's serialize the value and only update it if it is
        # different.
        if self.use_json:
            serialized_value = self.adapter.dump_json(value)
        else:
            serialized_value = self.adapter.dump_python(value, mode="json")
        try:
            # We need to validate and dump back to get the exact JSON-friendly
            # type representation.  Otherwise dates, enums, and other types
            # won't match the serialized value.
            param = params.get(self.param_name)
            if self.use_json:
                previous_value = self.adapter.dump_json(self.adapter.validate_json(param or ""))
            else:
                previous_value = self.adapter.dump_python(
                    self.adapter.validate_python(param),
                    mode="json",
                )
        except ValueError:
            previous_value = self.default_value

        if serialized_value == previous_value:
            return []
        else:
            params[self.param_name] = serialized_value  # type: ignore
            return [self.signal_name]


_VALID_QS_NAME_RX = re.compile(r"^[a-zA-Z_\d][-a-zA-Z_\d]*$")
