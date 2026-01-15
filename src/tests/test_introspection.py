from collections import defaultdict
from typing import Annotated
from uuid import UUID, uuid4

from django.http import QueryDict
from django.test import TestCase
from django.utils.datastructures import MultiValueDict
from fision.todo.models import Item  # type: ignore[import-untyped]
from pydantic import BaseModel, Field, ValidationError

from djhtmx.introspection import (
    ModelConfig,
    ModelRelatedField,
    annotate_model,
    filter_parameters,
    get_related_fields,
    guess_pk_type,
    isinstance_safe,
    issubclass_safe,
    parse_request_data,
)


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


class TestModelConfig(TestCase):
    def test_model_config_creation(self):
        """Test ModelConfig dataclass creation."""
        config = ModelConfig(
            select_related=["field1", "field2"], prefetch_related=["related1", "related2"]
        )

        self.assertEqual(config.select_related, ["field1", "field2"])
        self.assertEqual(config.prefetch_related, ["related1", "related2"])

    def test_model_config_defaults(self):
        """Test ModelConfig with default values."""
        config = ModelConfig()

        self.assertIsNone(config.select_related)
        self.assertIsNone(config.prefetch_related)


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


class TestOptionalModelHandling(TestCase):
    """Test that Model | None annotations handle non-existent objects correctly."""

    def test_optional_model_with_nonexistent_id(self):
        """Test that Model | None returns None when object doesn't exist."""

        # Define a test model with optional Item field
        class TestModel(BaseModel):
            class Config:
                arbitrary_types_allowed = True

            item: annotate_model(Item | None)  # type: ignore

        # Generate a UUID that definitely doesn't exist
        nonexistent_id = uuid4()

        # This should return None instead of raising DoesNotExist
        result = TestModel(item=nonexistent_id)
        self.assertIsNone(result.item)

    def test_required_model_with_nonexistent_id(self):
        """Test that required Model raises ValidationError when object doesn't exist."""

        # Define a test model with required Item field
        class TestModel(BaseModel):
            class Config:
                arbitrary_types_allowed = True

            item: annotate_model(Item)  # type: ignore

        # Generate a UUID that definitely doesn't exist
        nonexistent_id = uuid4()

        # This should raise a ValidationError (not DoesNotExist)
        with self.assertRaises(ValidationError):
            TestModel(item=nonexistent_id)

    def test_optional_model_with_explicit_none(self):
        """Test that Model | None accepts explicit None values."""

        class TestModel(BaseModel):
            class Config:
                arbitrary_types_allowed = True

            item: annotate_model(Item | None)  # type: ignore

        # Explicit None should work
        result = TestModel(item=None)
        self.assertIsNone(result.item)

    def test_optional_model_with_existing_id(self):
        """Test that Model | None loads existing objects correctly."""

        # Create a real item
        item = Item.objects.create(text="Test item")

        class TestModel(BaseModel):
            class Config:
                arbitrary_types_allowed = True

            item: annotate_model(Item | None)  # type: ignore

        # Should load the actual item
        result = TestModel(item=item.id)
        self.assertIsNotNone(result.item)
        self.assertEqual(result.item.id, item.id)
        self.assertEqual(result.item.text, "Test item")


class TestOptionalModelHandlingLazy(TestCase):
    """Test that Model | None annotations with lazy loading handle non-existent objects correctly."""

    def test_lazy_optional_model_with_nonexistent_id(self):
        """Test that lazy Model | None returns None when object doesn't exist."""

        # Define a test model with lazy optional Item field
        class TestModel(BaseModel):
            class Config:
                arbitrary_types_allowed = True

            item: annotate_model(Item | None, model_config=ModelConfig(lazy=True))  # type: ignore

        # Generate a UUID that definitely doesn't exist
        nonexistent_id = uuid4()

        # This should create a lazy proxy
        result = TestModel(item=nonexistent_id)

        # The proxy should be created (not None initially)
        self.assertIsNotNone(result.item)

        # But accessing attributes should handle the non-existent object
        # (In this case, __ensure_instance will set it to None)
        # The pk should still be accessible
        self.assertEqual(result.item.pk, nonexistent_id)

    def test_lazy_optional_model_with_existing_id(self):
        """Test that lazy Model | None loads existing objects correctly."""

        # Create a real item
        item = Item.objects.create(text="Test lazy item")

        class TestModel(BaseModel):
            class Config:
                arbitrary_types_allowed = True

            item: annotate_model(Item | None, model_config=ModelConfig(lazy=True))  # type: ignore

        # Should create a lazy proxy
        result = TestModel(item=item.id)
        self.assertIsNotNone(result.item)

        # Accessing attributes should load the item
        self.assertEqual(result.item.text, "Test lazy item")
        self.assertEqual(result.item.id, item.id)

    def test_lazy_optional_model_with_explicit_none(self):
        """Test that lazy Model | None accepts explicit None values."""

        class TestModel(BaseModel):
            class Config:
                arbitrary_types_allowed = True

            item: annotate_model(Item | None, model_config=ModelConfig(lazy=True))  # type: ignore

        # Explicit None should work
        result = TestModel(item=None)
        self.assertIsNone(result.item)


class TestAnnotateModelWithComplexTypes(TestCase):
    """Test that annotate_model doesn't break non-Model types."""

    def test_annotate_model_with_defaultdict(self):
        """Test that defaultdict fields work correctly after our changes."""

        class TestModel(BaseModel):
            class Config:
                arbitrary_types_allowed = True

            # Simple defaultdict without annotation
            data1: defaultdict[str, set[str]] = Field(default_factory=lambda: defaultdict(set))

            # With Annotated wrapper (like Query would do)
            data2: Annotated[defaultdict[str, set[str]], "some_metadata"] = Field(
                default_factory=lambda: defaultdict(set)
            )

        # Test creation with default factory
        result = TestModel()
        self.assertIsInstance(result.data1, defaultdict)
        self.assertIsInstance(result.data2, defaultdict)

        # Test creation with explicit value
        test_dict = defaultdict(set)
        test_dict["key1"].add("value1")
        result2 = TestModel(data1=test_dict, data2=test_dict)
        self.assertEqual(result2.data1["key1"], {"value1"})
        self.assertEqual(result2.data2["key1"], {"value1"})

    def test_annotate_model_explicitly_on_defaultdict(self):
        """Test annotate_model() on defaultdict type directly."""

        # Test that annotate_model doesn't modify non-Model types
        original = Annotated[defaultdict[str, set[str]], "metadata"]
        result = annotate_model(original)

        # Should return the same thing
        self.assertEqual(result, original)

        # Test creating a model with the annotated type
        class TestModel(BaseModel):
            class Config:
                arbitrary_types_allowed = True

            data: annotate_model(Annotated[defaultdict[str, set[str]], "metadata"])  # type: ignore

        # Should work fine
        test_dict = defaultdict(set)
        test_dict["key1"].add("value1")
        result = TestModel(data=test_dict)
        self.assertEqual(result.data["key1"], {"value1"})


class TestLazyModelDeletedObjects(TestCase):
    """Test that lazy model fields handle deleted objects correctly using real components."""

    def test_lazy_required_model_deleted_object_raises_exception(self):
        """Test that required lazy model raises ObjectDoesNotExist when object is deleted."""
        from django.core.exceptions import ObjectDoesNotExist

        from djhtmx.component import HtmxComponent

        # Create an item
        item = Item.objects.create(text="Temporary item")
        item_id = item.id

        # Create a component with lazy required field - this is how users actually write it
        class TestComponent(HtmxComponent, public=False):
            item: Annotated[Item, ModelConfig(lazy=True)]

        # Create instance with item ID - components process annotations automatically
        component = TestComponent(id="test", hx_name="TestComponent", user=None, item=item_id)
        self.assertIsNotNone(component.item)

        # Delete the item
        item.delete()

        # Accessing .pk should still work (doesn't trigger database query)
        self.assertEqual(component.item.pk, item_id)

        # Just accessing/checking component.item should raise ObjectDoesNotExist
        with self.assertRaises(ObjectDoesNotExist) as cm:
            if component.item:  # Triggers __bool__ which checks existence
                pass

        self.assertIn("Item", str(cm.exception))
        self.assertIn("does not exist", str(cm.exception))

    def test_lazy_optional_model_deleted_object_returns_none(self):
        """Test that optional lazy model returns None when object is deleted."""
        from djhtmx.component import HtmxComponent

        # Create an item
        item = Item.objects.create(text="Temporary item")
        item_id = item.id

        # Create a component with lazy optional field
        class TestComponent(HtmxComponent, public=False):
            item: Annotated[Item | None, ModelConfig(lazy=True)]

        # Create instance with item ID
        component = TestComponent(id="test", hx_name="TestComponent", user=None, item=item_id)
        self.assertIsNotNone(component.item)

        # Delete the item
        item.delete()

        # Accessing .pk should still work
        self.assertEqual(component.item.pk, item_id)

        # Checking component.item should be falsy (triggers __bool__)
        self.assertFalse(component.item)  # Object deleted, proxy is falsy

        # Accessing fields should return None (graceful handling)
        self.assertIsNone(component.item.text)
        self.assertIsNone(component.item.completed)

    def test_lazy_model_deleted_object_pk_access_works(self):
        """Test that accessing .pk on deleted lazy models doesn't trigger exception."""
        from django.core.exceptions import ObjectDoesNotExist

        from djhtmx.component import HtmxComponent

        # Create an item
        item = Item.objects.create(text="Temporary item")
        item_id = item.id

        # Test with both required and optional fields
        class TestComponent(HtmxComponent, public=False):
            required_item: Annotated[Item, ModelConfig(lazy=True)]
            optional_item: Annotated[Item | None, ModelConfig(lazy=True)]

        # Create instance
        component = TestComponent(
            id="test",
            hx_name="TestComponent",
            user=None,
            required_item=item_id,
            optional_item=item_id,
        )

        # Delete the item
        item.delete()

        # Accessing .pk should work without triggering database query or exception
        self.assertEqual(component.required_item.pk, item_id)
        self.assertEqual(component.optional_item.pk, item_id)

        # But checking truthiness should behave differently:
        # Required: should raise
        with self.assertRaises(ObjectDoesNotExist):
            if component.required_item:
                pass

        # Optional: should be falsy
        self.assertFalse(component.optional_item)

    def test_lazy_required_model_nonexistent_id_raises_exception(self):
        """Test that required lazy model raises ObjectDoesNotExist for non-existent ID."""
        from django.core.exceptions import ObjectDoesNotExist

        from djhtmx.component import HtmxComponent

        # Generate a UUID that definitely doesn't exist
        nonexistent_id = uuid4()

        class TestComponent(HtmxComponent, public=False):
            item: Annotated[Item, ModelConfig(lazy=True)]

        # Create instance with non-existent ID
        component = TestComponent(
            id="test", hx_name="TestComponent", user=None, item=nonexistent_id
        )

        # Proxy should be created
        self.assertIsNotNone(component.item)

        # Accessing .pk should work
        self.assertEqual(component.item.pk, nonexistent_id)

        # Checking component.item should raise ObjectDoesNotExist
        with self.assertRaises(ObjectDoesNotExist) as cm:
            if component.item:  # Triggers __bool__
                pass

        self.assertIn("Item", str(cm.exception))
        self.assertIn("does not exist", str(cm.exception))

    def test_lazy_optional_model_nonexistent_id_returns_none(self):
        """Test that optional lazy model is falsy for non-existent ID."""
        from djhtmx.component import HtmxComponent

        # Generate a UUID that definitely doesn't exist
        nonexistent_id = uuid4()

        class TestComponent(HtmxComponent, public=False):
            item: Annotated[Item | None, ModelConfig(lazy=True)]

        # Create instance with non-existent ID
        component = TestComponent(
            id="test", hx_name="TestComponent", user=None, item=nonexistent_id
        )

        # Proxy should be created
        self.assertIsNotNone(component.item)

        # Accessing .pk should work
        self.assertEqual(component.item.pk, nonexistent_id)

        # Checking component.item should be falsy (object doesn't exist)
        self.assertFalse(component.item)

        # Accessing fields should return None
        self.assertIsNone(component.item.text)
        self.assertIsNone(component.item.completed)
