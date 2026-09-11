"""Tests for `djhtmx.query.QueryPatcher`."""

from typing import Annotated, Literal

from django.http import QueryDict
from django.test import TestCase
from fision.todo.models import Item  # type: ignore[import-untyped]
from pydantic import Field

from djhtmx.component import HtmxComponent, Query
from djhtmx.introspection import (
    ModelConfig,
    _ModelBeforeValidator,  # noqa: PLC2701  (white-box test)
)
from djhtmx.query import QueryPatcher

# A PEP 695 type alias used as the type of a `Query` field.
type Category = Literal["area", "rank", "chapter"]


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

        # Round-trip the other way: a PK in the URL is parsed to the pk; the
        # instance itself is resolved later during build (the validator is pure).
        update = patcher.get_update_for_state(params)
        self.assertEqual(update[patcher.field_name], self.item.pk)

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


class TestQueryPatcherTypeAlias(TestCase):
    """Regression: a `Query` field typed with a PEP 695 type alias must work.

    `type Category = Literal[...]` is a `TypeAliasType` wrapper, not the bare
    `Literal`.  The patcher's simple-type gate has to unwrap it; otherwise it
    rejects the field with ``Invalid type annotation ... for a query string``.
    """

    def test_patcher_round_trips_literal_alias(self):
        class _Concrete(HtmxComponent, public=False):
            _template_name = "_Concrete.html"
            category: Annotated[Category, Query("c"), Field(default="area")]

        [patcher] = list(QueryPatcher.for_component(_Concrete))
        self.assertEqual(patcher.field_name, "category")
        self.assertEqual(patcher.default_value, "area")

        # A non-default value is written to the URL and resurrected from it.
        params = QueryDict("", mutable=True)
        patcher.get_updates_for_params("rank", params)
        self.assertEqual(params[patcher.param_name], "rank")

        update = patcher.get_update_for_state(params)
        self.assertEqual(update[patcher.field_name], "rank")


class TestQueryPatcherInvalidValue(TestCase):
    """Regression: a malformed value in the URL for a model-typed `Query` field
    must fall back to the default instead of raising.

    Resolving a model from its PK runs the field's adapter, and a value the PK
    field can't parse (here a non-numeric id) raises Django's `ValidationError`,
    which is *not* a `ValueError`.  Both query entry points have to swallow it.
    """

    @classmethod
    def setUpTestData(cls):
        cls.item = Item.objects.create(text="hello")

    def _patcher(self):
        class _Concrete(HtmxComponent, public=False):
            _template_name = "_Concrete.html"
            editing: Annotated[Item | None, Query("editing"), Field(default=None)]

        [patcher] = list(QueryPatcher.for_component(_Concrete))
        return patcher

    def test_get_update_for_state_falls_back_on_malformed_value(self):
        patcher = self._patcher()
        params = QueryDict(f"{patcher.param_name}=not-an-id", mutable=True)
        # Must not raise; the malformed value is ignored so the default applies.
        self.assertEqual(patcher.get_update_for_state(params), {})

    def test_get_updates_for_params_falls_back_on_malformed_existing_value(self):
        patcher = self._patcher()
        # The URL already holds a malformed value; writing a fresh value must
        # not choke on parsing the stale one back for comparison.
        params = QueryDict(f"{patcher.param_name}=not-an-id", mutable=True)
        signals = patcher.get_updates_for_params(self.item, params)
        self.assertEqual(params[patcher.param_name], str(self.item.pk))
        self.assertEqual(signals, [patcher.signal_name])


class TestQueryPatcherLazyModelField(TestCase):
    """Regression: a `ModelConfig(lazy=True)` Model field must work as a `Query` field.

    The annotation gate sees `_LazyModelProxy[Item] | None` rather than the model itself, so it
    needs a branch of its own.  Without it the field is refused with ``Invalid type annotation ...
    for a query string`` while the class is being defined, which takes down the whole module
    declaring the component.

    Lazy is the case the pk-in-the-URL design fits best: a proxy knows its pk without fetching the
    row, so neither direction of the round-trip costs a query.
    """

    @classmethod
    def setUpTestData(cls):
        cls.item = Item.objects.create(text="hello")

    def _patcher(self):
        class _Concrete(HtmxComponent, public=False):
            _template_name = "_Concrete.html"
            editing: Annotated[
                Item | None, ModelConfig(lazy=True), Query("editing"), Field(default=None)
            ]

        [patcher] = list(QueryPatcher.for_component(_Concrete))
        return patcher

    def _proxy(self):
        """The value a lazy field holds: whatever its validator builds out of a pk."""
        return _ModelBeforeValidator(Item, ModelConfig(lazy=True), allow_none=True)(self.item.pk)

    def test_patcher_writes_the_pk_without_fetching_the_row(self):
        patcher = self._patcher()
        proxy = self._proxy()
        params = QueryDict("", mutable=True)
        # The pk is already on the proxy, and the patcher compares the value with `is not None`
        # instead of asking for its truthiness -- truthiness resolves the row, by design.
        with self.assertNumQueries(0):
            patcher.get_updates_for_params(proxy, params)
        self.assertEqual(params[patcher.param_name], str(self.item.pk))

    def test_patcher_reads_the_pk_back_from_the_url(self):
        patcher = self._patcher()
        params = QueryDict(f"{patcher.param_name}={self.item.pk}", mutable=True)
        # The pk reaches the state as a pk; the proxy wrapping it is built by the field validator
        # during build, not here.
        self.assertEqual(patcher.get_update_for_state(params), {patcher.field_name: self.item.pk})
