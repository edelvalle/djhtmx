from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated, Any

from django.core.exceptions import ValidationError
from django.http import QueryDict
from pydantic import BaseModel, TypeAdapter
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

from djhtmx.introspection import (
    _ModelBeforeValidator,
    get_annotation_adapter,
    guess_pk_type,
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

    # When the field is a single `models.Model`, the URL carries its pk and
    # `adapter` is a *pk* adapter (str <-> pk).  The component value is an
    # instance, so we map instance->pk on the way out; the pk->instance load
    # happens in the field validator during build, on the pool thread.
    is_model: bool = False

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

                # Use the full annotation (with the validator/serializer that `annotate_model`
                # attaches for Model and QuerySet types) so the adapter knows how to convert between
                # the model instance and its PK in the URL.
                #
                # We reconstruct it from `field.annotation` + `field.metadata` rather than reading
                # `component.__annotations__[field_name]`, because `cls.__annotations__` is the
                # class's own dict and is *not* walked along the MRO.  A field declared on an
                # abstract base and inherited by a concrete component would otherwise fall through
                # to the bare `field.annotation` and drop the PlainSerializer — `dump_python` then
                # fails to serialise the model instance.
                full_annotation = (
                    Annotated[field.annotation, *field.metadata]
                    if field.metadata
                    else field.annotation
                )

                # A single Model field is carried in the URL as its pk.  Use a
                # pk adapter (the Model validator is pure and would reject a pk),
                # and let build resolve the pk back to an instance.
                # Keyed on the validator function, not on the pydantic wrapper `_Model` happens
                # to use for it, so swapping that wrapper cannot silently turn this detection off.
                model_validator = next(
                    (
                        meta.func
                        for meta in field.metadata
                        if isinstance(getattr(meta, "func", None), _ModelBeforeValidator)
                    ),
                    None,
                )
                if model_validator is not None:
                    adapter = TypeAdapter(guess_pk_type(model_validator.model) | None)
                    is_model = True
                else:
                    adapter = get_annotation_adapter(full_annotation)
                    is_model = False

                yield cls(
                    field_name=field_name,
                    param_name=param_name,
                    signal_name=f"querystring.{param_name}",
                    auto_subscribe=query.shared and query.auto_subscribe,
                    default_value=field.get_default(call_default_factory=True),
                    adapter=adapter,
                    use_json=is_collection_annotation(annotation),
                    is_model=is_model,
                )

    def get_update_for_state(self, params: QueryDict):
        if (raw_param := params.get(self.param_name)) is not None:
            # We need to perform the validation during patching, otherwise
            # ill-formed values in the query will raise, but we should just
            # simply ignore invalid values.  Pydantic raises a ValueError
            # subclass; model-resolving adapters (a PK in the URL that fails to
            # parse or match a row) raise Django's ValidationError, which is
            # *not* a ValueError -- catch both.
            try:
                return {
                    self.field_name: self.adapter.validate_json(raw_param)
                    if self.use_json
                    else self.adapter.validate_python(raw_param)
                }
            except (ValueError, ValidationError):
                # Preserve the last good known state in the component
                return {}
        else:
            return {self.field_name: self.default_value}

    def get_updates_for_params(self, value: Any, params: QueryDict) -> list[str]:
        # For a Model field the URL holds the pk; the component value is an
        # instance, so reduce it to its pk before (de)serialising with the pk
        # adapter.  `default_value` for these fields is None (no instance).
        if self.is_model:
            value = value.pk if value is not None else None

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
        except (ValueError, ValidationError):
            # A malformed value already sitting in the URL can't be parsed back;
            # treat it as the default so the fresh value overwrites it.  Django's
            # ValidationError (from model-resolving adapters) is not a ValueError.
            previous_value = self.default_value

        if serialized_value == previous_value:
            return []
        else:
            params[self.param_name] = serialized_value  # type: ignore
            return [self.signal_name]


_VALID_QS_NAME_RX = re.compile(r"^[a-zA-Z_\d][-a-zA-Z_\d]*$")
