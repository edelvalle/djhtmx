from __future__ import annotations

import contextlib
import copy
import datetime
import enum
import re
import types
import typing as t
from dataclasses import dataclass
from datetime import date
from uuid import UUID

import pydantic
from django.db import models
from django.http import QueryDict
from pydantic import BeforeValidator, PlainSerializer
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

from djhtmx.introspection import annotate_model, get_field_info, issubclass_safe


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

    #: Control where this parameter is shared or component-specific.
    shared: bool = True
    ns_attr_name: str = "hx_name_scrambled"
    auto_subscribe: bool = True

    def __post_init__(self):
        assert _VALID_QS_NAME_RX.match(self.name) is not None

    @property
    def subscription_channel(self):
        from .utils import get_query_subscription

        return get_query_subscription(self)


@dataclass(slots=True)
class QueryPatcher:
    """Helper to track the query string."""

    qs_arg: str
    field_name: str
    shared: bool
    auto_subscribe: bool

    _get_shared_value: t.Callable[[QueryDict], dict[str, t.Any]]
    _get_private_value: t.Callable[[QueryDict, str], dict[str, t.Any]]

    _set_shared_value: t.Callable[[QueryDict, t.Any], None]
    _set_private_value: t.Callable[[QueryDict, t.Any, str], None]

    ns_attr_name: str

    @property
    def subscription_channel(self):
        from .utils import get_query_subscription

        return get_query_subscription(self)

    @contextlib.contextmanager
    def tracking_query_string(self, repository, component):
        previous = getattr(component, self.field_name, (unset := object()))
        if previous is not unset:
            # Make a copy of the data, so that mutable types (e.g lists) can
            # be mutated and still tracked.
            previous = copy.copy(previous)
        yield
        after = getattr(component, self.field_name, unset)
        if previous != after:
            if self.shared:
                self._set_shared_value(repository.params, after)
            elif ns := getattr(component, self.ns_attr_name, component.hx_name_scrambled):
                self._set_private_value(repository.params, after, ns)
            repository.signals.add(self.subscription_channel)

    def get_shared_state_updates(self, qdict: QueryDict):
        return self._get_shared_value(qdict)

    def get_private_state_updates(self, qdict: QueryDict, suffix):
        return self._get_private_value(qdict, suffix)

    @classmethod
    def from_field_info(cls, field_name: str, annotation: Query, f: FieldInfo):
        return cls._from_field_info(annotation.name, field_name, f, annotation)

    @classmethod
    def _from_field_info(
        cls,
        qs_arg: str,
        field_name: str,
        f: FieldInfo,
        query_annotation: Query,
    ):
        def _maybe_extract_optional(ann):
            # Possibly extract t.Optional[sequence_type]
            if t.get_origin(ann) is types.UnionType:
                args = [arg for arg in t.get_args(ann) if ann is not types.NoneType]
                if len(args) == 1:
                    return args[0]
            return ann

        def _is_simple_type(ann):
            return (
                ann in _SIMPLE_TYPES
                or issubclass_safe(ann, models.Model)
                or issubclass_safe(ann, (enum.IntEnum, enum.StrEnum))
            )

        def _is_union_of_simple_types(ann):
            if t.get_origin(ann) is types.UnionType:
                return all(_is_simple_type(arg) for arg in t.get_args(ann))
            return False

        def _is_seq_of_simple_types(ann):
            ann = _maybe_extract_optional(ann)
            if t.get_origin(ann) in _SEQUENCE_ANNOTATIONS:
                try:
                    [arg] = t.get_args(ann)
                except ValueError:
                    return False
                return _is_simple_type(arg)
            if t.get_origin(ann) in (tuple, t.Tuple):
                try:
                    [arg, ellipsis] = t.get_args(ann)
                except ValueError:
                    return False
                return ellipsis is Ellipsis and _is_simple_type(arg)
            return False

        def _get_value_extractor(ann):
            if _is_simple_type(ann) or _is_union_of_simple_types(ann):
                getter = QueryDict.get
            elif _is_seq_of_simple_types(ann):
                getter = QueryDict.getlist
            else:
                raise TypeError(f"Invalid type annotation {ann} for a query string")

            def result(qd, suffix):
                if suffix:
                    return getter(qd, f"{qs_arg}__{suffix}")
                else:
                    return getter(qd, qs_arg)

            return result

        def _get_annotation_adapter(annotation):
            if f.annotation is bool:
                # compacted adapter for booleans ('t', 'f', infallible)
                return pydantic.TypeAdapter(
                    t.Annotated[
                        bool,
                        BeforeValidator(lambda v: True if v == "t" else False),
                        PlainSerializer(lambda v: "t" if v else "f"),
                    ]
                )

            return pydantic.TypeAdapter(
                t.Optional[annotate_model(f.annotation)]  # type: ignore
            )

        # NB: We need to perform the serialization during patching, otherwise
        # ill-formed values in the query will cause a Pydantic
        # ValidationError, but we should just simply ignore invalid values.
        extract_value = _get_value_extractor(f.annotation)
        adapter = _get_annotation_adapter(f.annotation)

        def _get_value(qdict: QueryDict, suffix: str = ""):
            if qs_value := extract_value(qdict, suffix):
                try:
                    return {field_name: adapter.validate_python(qs_value)}
                except pydantic.ValidationError:
                    pass
            elif f.default is not PydanticUndefined:
                return {field_name: f.default}
            elif f.default_factory is not None:
                return {field_name: f.default_factory()}

            return {}

        if _is_seq_of_simple_types(f.annotation):
            default_value = f.default

            def _set_value(qdict: QueryDict, value, suffix: str = ""):
                serial = adapter.dump_python(value)
                qs_param = f"{qs_arg}__{suffix}" if suffix else qs_arg
                if not serial or value == default_value:
                    qdict.pop(qs_param, None)
                else:
                    qdict.setlist(qs_param, serial)
        else:
            default_value = f.default

            def _set_value(qdict: QueryDict, value, suffix: str = ""):
                serial = adapter.dump_python(value)
                qs_param = f"{qs_arg}__{suffix}" if suffix else qs_arg
                if not serial or value == default_value:
                    qdict.pop(qs_param, None)
                else:
                    qdict[qs_param] = serial

        return cls(
            qs_arg,
            field_name,
            shared=query_annotation.shared,
            ns_attr_name=query_annotation.ns_attr_name,
            auto_subscribe=query_annotation.auto_subscribe,
            _get_shared_value=_get_value,
            _get_private_value=_get_value,
            _set_shared_value=_set_value,
            _set_private_value=_set_value,
        )

    @classmethod
    def for_component(cls, component_cls):
        def _field_has_default(f: FieldInfo):
            return f.default is not PydanticUndefined or f.default_factory is not None

        def _get_querystring_args(name, f: FieldInfo):
            done = False
            for meta in f.metadata:
                if isinstance(meta, Query):
                    if done:
                        raise TypeError(
                            f"Field '{name}' in component {cls.__qualname__} "
                            " has more than one Query annotation."
                        )
                    if not _field_has_default(f):
                        raise TypeError(
                            f"Field '{name}' of {cls.__qualname__} must have "
                            "a default or default_factory."
                        )

                    yield meta
                    done = True

        def _get_annotated_fields():
            seen = set()
            hints = t.get_type_hints(
                component_cls,
                include_extras=True,
            )
            for name, ann_type in hints.items():
                f = get_field_info(component_cls, name, ann_type)
                for qs_annotation in _get_querystring_args(name, f):
                    qs_arg = qs_annotation.name
                    if qs_arg in seen:
                        raise TypeError(
                            f"Component {cls.__qualname__} has multiple "
                            f"fields with the same query param '{qs_arg}'"
                        )
                    seen.add(qs_arg)
                    yield QueryPatcher.from_field_info(name, qs_annotation, f)

        try:
            return list(_get_annotated_fields())
        except TypeError as cause:
            raise TypeError(f"Invalid query string annotations in {component_cls}") from cause


_VALID_QS_NAME_RX = re.compile(r"^[a-zA-Z\d][-_a-zA-Z\d]*$")
_SEQUENCE_ANNOTATIONS = (
    list,
    set,
    frozenset,
    t.List,
    t.Sequence,
    t.MutableSet,
    t.Set,
    t.FrozenSet,
)
_SIMPLE_TYPES = (int, str, float, UUID, types.NoneType, date, datetime, bool)
