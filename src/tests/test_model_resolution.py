"""Model-typed fields are resolved (pk -> instance) during build.

The field validator resolves Model fields with the sync ORM during construction
(on the sync-work pool thread, here the test thread), so the built component
holds a concrete instance.  Constructing directly from a bare pk works too — the
validator resolves it; a missing pk yields None for an optional field.
"""

from django.http import QueryDict
from django.test import TestCase
from fision.todo.models import Item  # type: ignore[import-untyped]

from djhtmx.component import HtmxComponent
from djhtmx.repo import Repository, Session


class ModelProbe(HtmxComponent):
    _template_name = "ModelProbe.html"
    item: Item | None = None


class ModelResolutionTest(TestCase):
    def _build(self, state: dict):
        repo = Repository(
            user=None, session=Session(Repository.new_session_id()), params=QueryDict()
        )
        return repo.build("ModelProbe", state, retrieve_state=False)

    def test_build_resolves_model_field(self):
        item = Item.objects.create(text="hello")
        component = self._build({"id": "p1", "item": str(item.pk)})
        self.assertEqual(component.item, item)
        # Already resolved: touching it does not query.
        with self.assertNumQueries(0):
            self.assertEqual(component.item.text, "hello")

    def test_build_missing_model_is_none(self):
        from uuid import uuid4

        component = self._build({"id": "p2", "item": str(uuid4())})
        self.assertIsNone(component.item)

    def test_validator_resolves_bare_pk(self):
        # Constructing directly from a pk works again: the validator resolves it.
        item = Item.objects.create(text="direct")
        component = ModelProbe(id="p3", hx_name="ModelProbe", user=None, item=item.pk)
        self.assertEqual(component.item, item)

    def test_validator_bare_pk_missing_is_none(self):
        from uuid import uuid4

        component = ModelProbe(id="p4", hx_name="ModelProbe", user=None, item=uuid4())
        self.assertIsNone(component.item)


class LazyModelFieldTest(TestCase):
    """`ModelConfig(lazy=True)` wraps the pk in a proxy that fetches the row only
    on first attribute access, then caches it."""

    def _validator(self, *, lazy: bool, allow_none: bool = False):
        from djhtmx.introspection import (
            ModelConfig,
            _ModelBeforeValidator,  # noqa: PLC2701  (white-box test)
        )

        return _ModelBeforeValidator(Item, ModelConfig(lazy=lazy), allow_none=allow_none)

    def test_lazy_defers_query_until_access(self):
        item = Item.objects.create(text="lazy")
        proxy = self._validator(lazy=True)(str(item.pk))

        # Building the proxy and reading its pk does not query.
        with self.assertNumQueries(0):
            self.assertEqual(proxy.pk, item.pk)
        # First real attribute access loads the row…
        with self.assertNumQueries(1):
            self.assertEqual(proxy.text, "lazy")
        # …and it is cached thereafter.
        with self.assertNumQueries(0):
            self.assertEqual(proxy.text, "lazy")

    def test_lazy_proxy_serializes_back_to_pk(self):
        from djhtmx.introspection import _ModelPlainSerializer  # noqa: PLC2701  (white-box test)

        item = Item.objects.create(text="lazy")
        proxy = self._validator(lazy=True)(str(item.pk))
        serializer = _ModelPlainSerializer(Item)
        with self.assertNumQueries(0):
            self.assertEqual(serializer(proxy), item.pk)

    def test_eager_validator_resolves_immediately(self):
        item = Item.objects.create(text="eager")
        with self.assertNumQueries(1):
            resolved = self._validator(lazy=False)(str(item.pk))
        self.assertEqual(resolved, item)
