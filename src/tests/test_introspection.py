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
