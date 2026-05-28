"""Tests for `djhtmx.query.QueryPatcher`."""

from typing import Annotated

from django.http import QueryDict
from django.test import TestCase
from fision.todo.models import Item  # type: ignore[import-untyped]
from pydantic import Field

from djhtmx.component import HtmxComponent, Query
from djhtmx.query import QueryPatcher


class TestQueryPatcherInheritedModelField(TestCase):
    """Regression: a Model-typed `Query` field declared on an abstract base
    must still serialize properly when used through a concrete subclass.

    `cls.__annotations__` is the class's *own* dict and is not walked along the
    MRO, so the patcher needs to reconstruct the full annotation from the
    pydantic ``FieldInfo`` (``annotation`` + ``metadata``).  Otherwise the
    fallback drops the ``PlainSerializer`` added by ``annotate_model`` and
    ``dump_python`` blows up on the model instance.
    """

    @classmethod
    def setUpTestData(cls):
        cls.item = Item.objects.create(text="hello")

    def test_patcher_serializes_model_pk_for_inherited_field(self):
        class _AbstractBase(HtmxComponent, public=False):
            editing: Annotated[Item | None, Query("editing"), Field(default=None)]

        class _Concrete(_AbstractBase):
            _template_name = "_Concrete.html"

        [patcher] = list(QueryPatcher.for_component(_Concrete))
        self.assertEqual(patcher.field_name, "editing")

        # Round-trip: serializing a model instance must write its PK to the URL.
        params = QueryDict("", mutable=True)
        patcher.get_updates_for_params(self.item, params)
        self.assertEqual(params[patcher.param_name], str(self.item.pk))

        # Round-trip the other way: a PK in the URL must resurrect the instance.
        update = patcher.get_update_for_state(params)
        self.assertEqual(update[patcher.field_name], self.item)

    def test_patcher_serializes_model_pk_for_own_field(self):
        # Sanity check: the same scenario works when the field is declared
        # directly on the public component (no inheritance involved).
        class _Concrete(HtmxComponent, public=False):
            _template_name = "_Concrete.html"
            editing: Annotated[Item | None, Query("editing"), Field(default=None)]

        [patcher] = list(QueryPatcher.for_component(_Concrete))
        params = QueryDict("", mutable=True)
        patcher.get_updates_for_params(self.item, params)
        self.assertEqual(params[patcher.param_name], str(self.item.pk))
