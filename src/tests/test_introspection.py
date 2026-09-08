from typing import Literal
from uuid import UUID

from django.http import QueryDict
from django.test import TestCase
from django.utils.datastructures import MultiValueDict
from fision.todo.models import Item  # type: ignore[import-untyped]

from djhtmx.introspection import (
    ModelConfig,
    ModelRelatedField,
    annotate_model,
    filter_parameters,
    get_related_fields,
    guess_pk_type,
    is_collection_annotation,
    is_field_name_sequence,
    is_simple_annotation,
    isinstance_safe,
    issubclass_safe,
    parse_request_data,
)

# PEP 695 type aliases (the `type` statement) wrap the real type in a
# ``TypeAliasType``; ``_unwrap_annotated`` must peel them, including when nested.
type _CategoryAlias = Literal["area", "rank", "chapter"]
type _NestedAlias = _CategoryAlias
type _UnionAlias = int | None
type _CollectionAlias = list[str]


class TestParseRequestData(TestCase):
    def test_parse_request_data_simple_dict(self):
        """Test parse_request_data with simple dictionary."""
        data = {"key": "value", "number": "42"}
        result = parse_request_data(data)

        self.assertEqual(result["key"], "value")
        self.assertEqual(result["number"], "42")

    def test_parse_request_data_multivalue_dict(self):
        """Test parse_request_data with MultiValueDict."""
        data = MultiValueDict({
            "single": ["value"],
            "multiple": ["val1", "val2"],
            "list_notation[]": ["item1", "item2", "item3"],
        })

        result = parse_request_data(data)

        self.assertEqual(result["single"], "value")
        # MultiValueDict takes the last value, not all values
        self.assertEqual(result["multiple"], "val2")
        self.assertEqual(result["list_notation"], ["item1", "item2", "item3"])

    def test_parse_request_data_query_dict(self):
        """Test parse_request_data with QueryDict."""
        data = QueryDict("key=value&number=42&list[]=a&list[]=b")

        result = parse_request_data(data)

        self.assertEqual(result["key"], "value")
        self.assertEqual(result["number"], "42")
        self.assertEqual(result["list"], ["a", "b"])

    def test_parse_request_data_empty_values(self):
        """Test parse_request_data handles empty values."""
        data = MultiValueDict({"empty_string": [""], "empty_list": [], "none_value": [None]})

        result = parse_request_data(data)

        self.assertEqual(result["empty_string"], "")
        # Empty list becomes None, not absent
        self.assertIsNone(result["empty_list"])
        self.assertIsNone(result["none_value"])


class TestSimpleAnnotationWithTypeAlias(TestCase):
    """Regression: PEP 695 type aliases must be unwrapped to their value.

    A ``type Alias = Literal[...]`` statement yields a ``TypeAliasType`` whose
    real type lives in ``__value__``.  ``_unwrap_annotated`` has to peel it (and
    nested aliases) so the simple/collection checks see the underlying type.
    """

    def test_literal_alias_is_simple(self):
        self.assertTrue(is_simple_annotation(_CategoryAlias))

    def test_nested_alias_is_simple(self):
        self.assertTrue(is_simple_annotation(_NestedAlias))

    def test_union_alias_is_simple(self):
        self.assertTrue(is_simple_annotation(_UnionAlias))

    def test_collection_alias_is_simple_and_collection(self):
        self.assertTrue(is_simple_annotation(_CollectionAlias))
        self.assertTrue(is_collection_annotation(_CollectionAlias))


class TestModelConfig(TestCase):
    def test_model_config_creation(self):
        """Test ModelConfig dataclass creation, storing the lists it accepts as tuples."""
        config = ModelConfig(
            select_related=["field1", "field2"], prefetch_related=["related1", "related2"]
        )

        self.assertEqual(config.select_related, ("field1", "field2"))
        self.assertEqual(config.prefetch_related, ("related1", "related2"))

    def test_model_config_defaults(self):
        """Test ModelConfig with default values."""
        config = ModelConfig()

        self.assertIsNone(config.select_related)
        self.assertIsNone(config.prefetch_related)

    def test_model_config_accepts_any_sequence(self):
        """Any sequence of field names is accepted, and stored as a tuple."""
        from collections.abc import Sequence

        class Pair(Sequence):
            """A sequence that is neither a list nor a tuple, for the general contract."""

            def __getitem__(self, index):
                return ("a", "b")[index]

            def __len__(self):
                return 2

        self.assertEqual(ModelConfig(select_related=("a", "b")).select_related, ("a", "b"))
        self.assertEqual(ModelConfig(select_related=["a", "b"]).select_related, ("a", "b"))
        self.assertEqual(ModelConfig(select_related=Pair()).select_related, ("a", "b"))

    def test_model_config_rejects_a_bare_string(self):
        """A bare string is a `Sequence[str]`, so nothing else would catch it.

        No type checker objects to it, and iterating it asks for one related field per character --
        `select_related="owner"` would silently become `select_related("o", "w", ...)`.  Raise where
        the mistake is, instead of failing later inside the query with a puzzling field name.
        """
        for name in ("select_related", "prefetch_related"):
            with self.subTest(argument=name):
                with self.assertRaises(TypeError) as context:
                    ModelConfig(**{name: "content_type"})
                # The message has to name the argument and offer the fix.
                self.assertIn(name, str(context.exception))
                self.assertIn("('content_type',)", str(context.exception))

    def test_model_config_rejects_what_is_not_a_sequence(self):
        """Anything that cannot be a list of names is refused by the argument's own name."""
        with self.assertRaises(TypeError) as context:
            ModelConfig(select_related=42)  # type: ignore[arg-type]

        self.assertIn("select_related", str(context.exception))

    def test_is_field_name_sequence_separates_names_from_a_name(self):
        """The predicate behind the check: a string is the one sequence that is not a list of names."""
        self.assertTrue(is_field_name_sequence(("owner",)))
        self.assertTrue(is_field_name_sequence(["owner", "category"]))
        self.assertTrue(is_field_name_sequence(()))
        self.assertFalse(is_field_name_sequence("owner"))
        self.assertFalse(is_field_name_sequence(b"owner"))

    def test_model_config_is_hashable_when_built_from_lists(self):
        """The config is a cache key, so a list argument must not make it unhashable.

        A component annotated with the documented list form used to raise `TypeError: unhashable
        type: 'list'` while its annotation was being built -- at class-definition time, so importing
        the module was enough to bring the application down.
        """
        from typing import Annotated

        from djhtmx.component import HtmxComponent

        config = ModelConfig(lazy=True, select_related=["a"], prefetch_related=["b"])
        self.assertEqual(
            hash(config),
            hash(ModelConfig(lazy=True, select_related=("a",), prefetch_related=("b",))),
        )

        class ListConfiguredModel(HtmxComponent):
            _template_name = "ListConfiguredModel.html"
            item: Annotated[Item, ModelConfig(select_related=["id"], prefetch_related=["id"])]

        self.assertTrue(ListConfiguredModel.model_fields["item"])


class TestModelRelatedField(TestCase):
    def test_model_related_field_creation(self):
        """Test ModelRelatedField dataclass creation."""
        field = ModelRelatedField(
            name="items", relation_name="todo_list", related_model_name="Item"
        )

        self.assertEqual(field.name, "items")
        self.assertEqual(field.relation_name, "todo_list")
        self.assertEqual(field.related_model_name, "Item")


class TestGetRelatedFields(TestCase):
    def test_get_related_fields_for_model(self):
        """Test get_related_fields returns related fields for a model."""
        result = get_related_fields(Item)

        self.assertIsInstance(result, tuple)
        # Item model should have some related fields or none
        for field in result:
            self.assertIsInstance(field, ModelRelatedField)


class TestAnnotateModel(TestCase):
    def setUp(self):
        self.item = Item.objects.create(text="Test item")

    def test_annotate_model_basic(self):
        """Test annotate_model with basic model."""
        # This is primarily testing that the function doesn't crash
        adapter = annotate_model(Item)

        # The result is a type, not a validator
        self.assertIsNotNone(adapter)

    def test_annotate_model_returns_type(self):
        """Test annotate_model returns a type that can be used."""
        adapter = annotate_model(Item)

        # Should return some form of annotated type
        self.assertIsNotNone(adapter)


class TestUtilityFunctions(TestCase):
    def setUp(self):
        self.item = Item.objects.create(text="Test item for utilities")

    def test_guess_pk_type(self):
        """Test guess_pk_type returns correct type for model."""
        pk_type = guess_pk_type(Item)

        # Should return UUID type since Item uses UUID primary keys
        self.assertEqual(pk_type, UUID)

    def test_isinstance_safe_with_valid_type(self):
        """Test isinstance_safe with valid type."""
        result = isinstance_safe("test", str)
        self.assertTrue(result)

        result = isinstance_safe(42, int)
        self.assertTrue(result)

    def test_isinstance_safe_with_invalid_type(self):
        """Test isinstance_safe with invalid type that might raise TypeError."""
        # Test with None type which could cause issues
        result = isinstance_safe("test", type(None))
        self.assertFalse(result)

    def test_issubclass_safe_with_valid_classes(self):
        """Test issubclass_safe with valid classes."""
        result = issubclass_safe(Item, object)
        self.assertTrue(result)

    def test_issubclass_safe_with_invalid_input(self):
        """Test issubclass_safe with invalid input that might raise TypeError."""
        # Test with string instead of class
        result = issubclass_safe("not_a_class", object)
        self.assertFalse(result)

    def test_filter_parameters_basic(self):
        """Test filter_parameters with simple function."""

        def test_func(a: int, b: str = "default"):
            return str(a) + b

        kwargs = {"a": 1, "b": "test", "extra": "ignored"}
        result = filter_parameters(test_func, kwargs)

        expected = {"a": 1, "b": "test"}
        self.assertEqual(result, expected)

    def test_filter_parameters_with_excess_args(self):
        """Test filter_parameters ignores excess arguments."""

        def test_func(x: int):
            return x

        kwargs = {"x": 42, "y": "ignored", "z": "also_ignored"}
        result = filter_parameters(test_func, kwargs)

        expected = {"x": 42}
        self.assertEqual(result, expected)


class TestComplexDataTypes(TestCase):
    def test_parse_request_data_with_simple_arrays(self):
        """Test parse_request_data with simple array notation."""
        data = MultiValueDict({
            "simple[0]": ["first"],
            "simple[1]": ["second"],
            "tags[]": ["python", "django", "htmx"],
        })

        result = parse_request_data(data)

        # Should handle simple array indexing
        self.assertEqual(result["simple"], ["first", "second"])
        self.assertEqual(result["tags"], ["python", "django", "htmx"])

    def test_parse_request_data_boolean_conversion(self):
        """Test parse_request_data handles various value types."""
        data = MultiValueDict({
            "true_val": ["true"],
            "false_val": ["false"],
            "on_val": ["on"],
            "off_val": ["off"],
            "empty_val": [""],
            "zero_val": ["0"],
        })

        result = parse_request_data(data)

        # These should be parsed as strings, not converted to booleans
        self.assertEqual(result["true_val"], "true")
        self.assertEqual(result["false_val"], "false")
        self.assertEqual(result["on_val"], "on")
        self.assertEqual(result["off_val"], "off")
        self.assertEqual(result["empty_val"], "")
        self.assertEqual(result["zero_val"], "0")


class TestOptionalModelInComponent(TestCase):
    """Model fields are resolved (pk -> instance) by their field validator during
    construction (sync ORM, on the pool thread).  A bare pk is resolved; a
    missing pk yields None for optional fields and raises for required ones."""

    def _build(self, component_class, state):
        return component_class(id="c", hx_name=component_class.__name__, user=None, **state)

    def test_optional_model_nonexistent_id(self):
        from uuid import uuid4

        from djhtmx.component import HtmxComponent

        class OptionalModelNonexistent(HtmxComponent):
            _template_name = "OptionalModelNonexistent.html"
            item: Item | None

        component = self._build(OptionalModelNonexistent, {"item": uuid4()})
        self.assertIsNone(component.item)

    def test_optional_model_deleted_id(self):
        from djhtmx.component import HtmxComponent

        item = Item.objects.create(text="To be deleted")
        item_id = item.id
        item.delete()

        class OptionalModelDeleted(HtmxComponent):
            _template_name = "OptionalModelDeleted.html"
            item: Item | None

        component = self._build(OptionalModelDeleted, {"item": item_id})
        self.assertIsNone(component.item)

    def test_optional_model_existing_id(self):
        from djhtmx.component import HtmxComponent

        item = Item.objects.create(text="Test item")

        class OptionalModelExisting(HtmxComponent):
            _template_name = "OptionalModelExisting.html"
            item: Item | None

        component = self._build(OptionalModelExisting, {"item": item.id})
        self.assertEqual(component.item, item)
        self.assertEqual(component.item.text, "Test item")

    def test_required_model_nonexistent_id_raises(self):
        from uuid import uuid4

        from pydantic import ValidationError

        from djhtmx.component import HtmxComponent

        class RequiredModelNonexistent(HtmxComponent):
            _template_name = "RequiredModelNonexistent.html"
            item: Item  # required

        with self.assertRaises(ValidationError) as ctx:
            self._build(RequiredModelNonexistent, {"item": uuid4()})
        self.assertIn("does not exist", str(ctx.exception))

    def test_validator_resolves_bare_pk(self):
        # Constructing directly from a pk works: the validator resolves it (an
        # existing pk -> instance; a missing optional pk -> None).
        from uuid import uuid4

        from djhtmx.component import HtmxComponent

        class DirectModel(HtmxComponent):
            _template_name = "DirectModel.html"
            item: Item | None

        item = Item.objects.create(text="direct")
        resolved = DirectModel(id="c", hx_name="DirectModel", user=None, item=item.pk)
        self.assertEqual(resolved.item, item)

        missing = DirectModel(id="c", hx_name="DirectModel", user=None, item=uuid4())
        self.assertIsNone(missing.item)

    def test_required_model_rejects_none(self):
        """A required Model field rejects None instead of holding it.

        The field validator runs *before* the core schema precisely so the declared type keeps
        meaning something at runtime: a plain validator would replace that schema, and the field
        would hold the None it returned unchanged -- a component reading `self.item` in a handler
        then fails far from the cause.
        """
        from pydantic import ValidationError

        from djhtmx.component import HtmxComponent

        class RequiredModelNone(HtmxComponent):
            _template_name = "RequiredModelNone.html"
            item: Item  # required

        with self.assertRaises(ValidationError) as ctx:
            self._build(RequiredModelNone, {"item": None})

        self.assertEqual(
            [(error["type"], error["loc"]) for error in ctx.exception.errors()],
            [("is_instance_of", ("item",))],
        )

    def test_required_model_accepts_a_pk_and_an_instance(self):
        """The control: enforcing the annotation must not reject what a component legitimately gets."""
        from djhtmx.component import HtmxComponent

        item = Item.objects.create(text="Test item")

        class RequiredModelAccepts(HtmxComponent):
            _template_name = "RequiredModelAccepts.html"
            item: Item

        self.assertEqual(self._build(RequiredModelAccepts, {"item": item.pk}).item, item)
        self.assertEqual(self._build(RequiredModelAccepts, {"item": item}).item, item)


class TestOptionalLazyModelInComponent(TestCase):
    """Test that HtmxComponent with lazy Model | None handles non-existent objects correctly."""

    def test_component_with_optional_lazy_model_nonexistent_id(self):
        """Test that component with lazy Model | None sets field to None when ID doesn't exist."""
        from typing import Annotated
        from uuid import uuid4

        from djhtmx.component import HtmxComponent
        from djhtmx.introspection import ModelConfig

        # Create a test component with optional lazy Item field
        class OptionalLazyModelNonexistent(HtmxComponent):
            _template_name = "OptionalLazyModelNonexistent.html"
            item: Annotated[Item | None, ModelConfig(lazy=True)]

        # Generate a UUID that doesn't exist in the database
        nonexistent_id = uuid4()

        # Build the component with the non-existent ID
        component = OptionalLazyModelNonexistent(
            id="test-component",
            hx_name="OptionalLazyModelNonexistent",
            user=None,
            item=nonexistent_id,
        )

        # The item field should be a lazy proxy, not None initially
        self.assertIsNotNone(component.item)

        # Accessing the pk should work without triggering database query
        self.assertEqual(component.item.pk, nonexistent_id)

        # Checking truthiness resolves the row and finds nothing, so the proxy is falsy
        self.assertFalse(component.item)

    def test_component_with_optional_lazy_model_deleted_id(self):
        """Test that component with lazy Model | None handles deleted objects."""
        from typing import Annotated

        from djhtmx.component import HtmxComponent
        from djhtmx.introspection import ModelConfig

        # Create an item and then delete it
        item = Item.objects.create(text="To be deleted")
        item_id = item.id
        item.delete()

        # Create a test component with optional lazy Item field
        class OptionalLazyModelDeleted(HtmxComponent):
            _template_name = "OptionalLazyModelDeleted.html"
            item: Annotated[Item | None, ModelConfig(lazy=True)]

        # Build the component with the deleted item's ID
        component = OptionalLazyModelDeleted(
            id="test-component",
            hx_name="OptionalLazyModelDeleted",
            user=None,
            item=item_id,
        )

        # The item field should be a lazy proxy
        self.assertIsNotNone(component.item)

        # Accessing the pk should work
        self.assertEqual(component.item.pk, item_id)

    def test_component_with_optional_lazy_model_existing_id(self):
        """Test that component with lazy Model | None loads existing objects correctly."""
        from typing import Annotated

        from djhtmx.component import HtmxComponent
        from djhtmx.introspection import ModelConfig

        # Create a real item
        item = Item.objects.create(text="Test lazy item")

        # Create a test component with optional lazy Item field
        class OptionalLazyModelExisting(HtmxComponent):
            _template_name = "OptionalLazyModelExisting.html"
            item: Annotated[Item | None, ModelConfig(lazy=True)]

        # Build the component with the existing item's ID
        component = OptionalLazyModelExisting(
            id="test-component",
            hx_name="OptionalLazyModelExisting",
            user=None,
            item=item.id,
        )

        # The item field should be a lazy proxy
        self.assertIsNotNone(component.item)

        # Accessing attributes should load the item
        self.assertEqual(component.item.text, "Test lazy item")
        self.assertEqual(component.item.id, item.id)

    def test_component_with_required_lazy_model_nonexistent_id(self):
        """Test that component with required lazy Model raises error when accessing non-existent object."""
        from typing import Annotated
        from uuid import uuid4

        from djhtmx.component import HtmxComponent
        from djhtmx.introspection import ModelConfig

        # Create a test component with required lazy Item field
        class RequiredLazyModelNonexistent(HtmxComponent):
            _template_name = "RequiredLazyModelNonexistent.html"
            item: Annotated[Item, ModelConfig(lazy=True)]  # Required, not optional

        # Generate a UUID that doesn't exist in the database
        nonexistent_id = uuid4()

        # Component creation should succeed (lazy loading)
        component = RequiredLazyModelNonexistent(
            id="test-component",
            hx_name="RequiredLazyModelNonexistent",
            user=None,
            item=nonexistent_id,
        )

        # The proxy should be created
        self.assertIsNotNone(component.item)

        # But accessing attributes should raise ValueError
        with self.assertRaises(ValueError) as context:
            _ = component.item.text  # Try to access an attribute

        # Verify the error message contains useful information
        error_str = str(context.exception)
        self.assertIn("Item", error_str)
        self.assertIn("does not exist", error_str)

        # Truthiness cannot answer for a required field either: the row is gone, and claiming the
        # proxy is truthy is what used to send callers on to the attribute access above.
        with self.assertRaises(ValueError):
            bool(component.item)

    def test_lazy_proxy_truthiness_tracks_the_row(self):
        """Truthiness must answer for the row, not for the proxy object.

        Without `__bool__` a proxy was truthy no matter what it wrapped, so the one question the
        check exists to ask -- is this thing there? -- always answered yes, deleted rows included.
        """
        from typing import Annotated

        from djhtmx.component import HtmxComponent
        from djhtmx.introspection import ModelConfig

        class TruthinessLazyModel(HtmxComponent):
            _template_name = "TruthinessLazyModel.html"
            item: Annotated[Item | None, ModelConfig(lazy=True)]

        item = Item.objects.create(text="Present")

        def build(value):
            return TruthinessLazyModel(
                id="test-component", hx_name="TruthinessLazyModel", user=None, item=value
            ).item

        self.assertTrue(build(item.pk))
        self.assertTrue(build(item))
        self.assertFalse(build(None))

        item.delete()
        self.assertFalse(build(item.pk))


class TestLazyModelRelatedFields(TestCase):
    """`ModelConfig`'s related-field arguments have to reach the query the proxy finally makes."""

    def test_select_related_saves_the_query_for_the_related_object(self):
        """The proxy fetches the row; if the config never reaches it, the JOIN never happens."""
        from typing import Annotated

        from django.contrib.auth.models import Permission

        from djhtmx.component import HtmxComponent
        from djhtmx.introspection import ModelConfig

        class SelectRelatedLazyModel(HtmxComponent):
            _template_name = "SelectRelatedLazyModel.html"
            permission: Annotated[
                Permission, ModelConfig(lazy=True, select_related=("content_type",))
            ]

        # Read the expected value up front: the comparison must not be what pays for the JOIN.
        permission = Permission.objects.select_related("content_type").first()
        assert permission is not None
        expected_app_label = permission.content_type.app_label

        component = SelectRelatedLazyModel(
            id="test-component",
            hx_name="SelectRelatedLazyModel",
            user=None,
            permission=permission.pk,
        )

        # One query resolves the row *and* its content type, because the config reached the proxy.
        with self.assertNumQueries(1):
            self.assertEqual(component.permission.pk, permission.pk)
            self.assertEqual(component.permission.content_type.app_label, expected_app_label)

    def test_prefetch_related_is_applied_when_the_row_is_fetched(self):
        from typing import Annotated

        from django.contrib.auth.models import Group, User

        from djhtmx.component import HtmxComponent
        from djhtmx.introspection import ModelConfig

        class PrefetchRelatedLazyModel(HtmxComponent):
            _template_name = "PrefetchRelatedLazyModel.html"
            member: Annotated[User, ModelConfig(lazy=True, prefetch_related=("groups",))]

        member = User.objects.create_user(username="member")
        member.groups.add(Group.objects.create(name="crew"))

        component = PrefetchRelatedLazyModel(
            id="test-component", hx_name="PrefetchRelatedLazyModel", user=None, member=member.pk
        )

        # Two queries: the row, plus the prefetch the config asked for.  Reading the groups a second
        # time hits the prefetch cache instead of the database, which is the whole point.
        with self.assertNumQueries(2):
            self.assertEqual([group.name for group in component.member.groups.all()], ["crew"])
            self.assertEqual([group.name for group in component.member.groups.all()], ["crew"])


class TestQuerySetInComponent(TestCase):
    """Test that HtmxComponent with QuerySet handles non-existent IDs correctly."""

    def test_component_with_queryset_nonexistent_ids(self):
        """Test that component with QuerySet returns empty queryset for non-existent IDs."""
        from uuid import uuid4

        from fision.todo.models import ItemQS

        from djhtmx.component import HtmxComponent

        # Create a test component with QuerySet field
        class QuerysetNonexistent(HtmxComponent):
            _template_name = "QuerysetNonexistent.html"
            items: ItemQS

        # Generate UUIDs that don't exist in the database
        nonexistent_ids = [uuid4(), uuid4(), uuid4()]

        # Build the component with non-existent IDs
        component = QuerysetNonexistent(
            id="test-component",
            hx_name="QuerysetNonexistent",
            user=None,
            items=nonexistent_ids,
        )

        # The items field should be an empty queryset
        self.assertIsInstance(component.items, ItemQS)
        self.assertEqual(component.items.count(), 0)
        self.assertEqual(list(component.items), [])

    def test_component_with_queryset_mixed_ids(self):
        """Test that component with QuerySet filters out non-existent IDs."""
        from uuid import uuid4

        from fision.todo.models import ItemQS

        from djhtmx.component import HtmxComponent

        # Create some real items
        item1 = Item.objects.create(text="Item 1")
        item2 = Item.objects.create(text="Item 2")

        # Create a test component with QuerySet field
        class QuerysetMixed(HtmxComponent):
            _template_name = "QuerysetMixed.html"
            items: ItemQS

        # Mix valid and invalid IDs
        mixed_ids = [item1.id, uuid4(), item2.id, uuid4()]

        # Build the component with mixed IDs
        component = QuerysetMixed(
            id="test-component",
            hx_name="QuerysetMixed",
            user=None,
            items=mixed_ids,
        )

        # The items field should only contain the valid items
        self.assertIsInstance(component.items, ItemQS)
        self.assertEqual(component.items.count(), 2)
        item_ids = {item.id for item in component.items}
        self.assertEqual(item_ids, {item1.id, item2.id})

    def test_component_with_queryset_deleted_ids(self):
        """Test that component with QuerySet excludes deleted items."""
        from fision.todo.models import ItemQS

        from djhtmx.component import HtmxComponent

        # Create items and then delete some
        item1 = Item.objects.create(text="Item 1")
        item2 = Item.objects.create(text="To be deleted")
        item3 = Item.objects.create(text="Item 3")

        item2_id = item2.id
        item2.delete()

        # Create a test component with QuerySet field
        class QuerysetDeleted(HtmxComponent):
            _template_name = "QuerysetDeleted.html"
            items: ItemQS

        # Try to use all IDs including the deleted one
        all_ids = [item1.id, item2_id, item3.id]

        # Build the component with IDs including deleted
        component = QuerysetDeleted(
            id="test-component",
            hx_name="QuerysetDeleted",
            user=None,
            items=all_ids,
        )

        # The items field should only contain items 1 and 3
        self.assertIsInstance(component.items, ItemQS)
        self.assertEqual(component.items.count(), 2)
        item_ids = {item.id for item in component.items}
        self.assertEqual(item_ids, {item1.id, item3.id})

    def test_component_with_queryset_existing_ids(self):
        """Test that component with QuerySet loads all existing items correctly."""
        from fision.todo.models import ItemQS

        from djhtmx.component import HtmxComponent

        # Create real items
        item1 = Item.objects.create(text="Item 1")
        item2 = Item.objects.create(text="Item 2")
        item3 = Item.objects.create(text="Item 3")

        # Create a test component with QuerySet field
        class QuerysetExisting(HtmxComponent):
            _template_name = "QuerysetExisting.html"
            items: ItemQS

        # Build the component with all valid IDs
        valid_ids = [item1.id, item2.id, item3.id]

        component = QuerysetExisting(
            id="test-component",
            hx_name="QuerysetExisting",
            user=None,
            items=valid_ids,
        )

        # The items field should contain all items
        self.assertIsInstance(component.items, ItemQS)
        self.assertEqual(component.items.count(), 3)
        item_ids = {item.id for item in component.items}
        self.assertEqual(item_ids, {item1.id, item2.id, item3.id})
