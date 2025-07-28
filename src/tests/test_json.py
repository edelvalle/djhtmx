import dataclasses
import enum
import json
from datetime import datetime

from django.test import TestCase
from fision.todo.models import Item  # type: ignore[import-untyped]
from pydantic import BaseModel

from djhtmx.json import HtmxEncoder, decode, default, dumps, encode, loads


class ExampleEnum(enum.Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"


@dataclasses.dataclass
class ExampleDataclass:
    name: str
    value: int


class ExamplePydanticModel(BaseModel):
    title: str
    count: int


class TestJSONFunctions(TestCase):
    def setUp(self):
        self.item = Item.objects.create(text="Test item")

    def test_loads_function(self):
        """Test loads function uses orjson."""
        json_str = '{"key": "value", "number": 42}'
        result = loads(json_str)

        self.assertEqual(result["key"], "value")
        self.assertEqual(result["number"], 42)

    def test_dumps_function(self):
        """Test dumps function produces JSON string."""
        data = {"key": "value", "number": 42}
        result = dumps(data)

        self.assertIsInstance(result, str)
        parsed = json.loads(result)
        self.assertEqual(parsed["key"], "value")
        self.assertEqual(parsed["number"], 42)

    def test_encode_model_instance(self):
        """Test encode function serializes Django model."""
        result = encode(self.item)

        self.assertIsInstance(result, str)
        self.assertIn(str(self.item.pk), result)
        self.assertIn("Test item", result)

    def test_decode_model_instance(self):
        """Test decode function deserializes Django model."""
        encoded = encode(self.item)
        decoded = decode(encoded)

        self.assertEqual(decoded.pk, self.item.pk)
        self.assertEqual(decoded.text, "Test item")  # type: ignore[attr-defined]
        self.assertTrue(hasattr(decoded, "save"))

    def test_htmx_encoder_class(self):
        """Test HtmxEncoder delegates to default function."""
        encoder = HtmxEncoder()

        # Test with a model instance
        result = encoder.default(self.item)
        self.assertEqual(result, self.item.pk)


class TestDefaultFunction(TestCase):
    def setUp(self):
        self.item = Item.objects.create(text="Test item")

    def test_default_with_model_instance(self):
        """Test default function with Django model instance."""
        result = default(self.item)
        self.assertEqual(result, self.item.pk)

    def test_default_with_enum(self):
        """Test default function with enum."""
        result = default(ExampleEnum.ACTIVE)
        self.assertEqual(result, "active")

    def test_default_with_dataclass(self):
        """Test default function with dataclass."""
        dc = ExampleDataclass(name="test", value=42)
        result = default(dc)

        expected = {"name": "test", "value": 42}
        self.assertEqual(result, expected)

    def test_default_with_pydantic_model(self):
        """Test default function with Pydantic model."""
        model = ExamplePydanticModel(title="test", count=5)
        result = default(model)

        expected = {"title": "test", "count": 5}
        self.assertEqual(result, expected)

    def test_default_with_generator(self):
        """Test default function with generator."""

        def gen():
            yield 1
            yield 2
            yield 3

        result = default(gen())
        self.assertEqual(result, [1, 2, 3])

    def test_default_with_set(self):
        """Test default function with set."""
        test_set = {1, 2, 3}
        result = default(test_set)

        self.assertIsInstance(result, list)
        self.assertEqual(set(result), test_set)

    def test_default_with_frozenset(self):
        """Test default function with frozenset."""
        test_frozenset = frozenset([1, 2, 3])
        result = default(test_frozenset)

        self.assertIsInstance(result, list)
        self.assertEqual(set(result), set(test_frozenset))

    def test_default_with_custom_json_method(self):
        """Test default function with object having __json__ method."""

        class CustomObject:
            def __json__(self):
                return {"custom": "serialization"}

        obj = CustomObject()
        result = default(obj)

        self.assertEqual(result, {"custom": "serialization"})

    def test_default_with_datetime_fallback(self):
        """Test default function falls back to Django encoder for datetime."""
        dt = datetime(2023, 1, 15, 12, 30, 45)
        result = default(dt)

        # Should use Django's JSON encoder
        self.assertIsInstance(result, str)
        self.assertIn("2023-01-15", result)

    def test_default_with_unsupported_type_raises_error(self):
        """Test default function raises TypeError for unsupported types."""

        class UnsupportedType:
            pass

        obj = UnsupportedType()

        with self.assertRaises(TypeError):
            default(obj)


class TestDumpsIntegration(TestCase):
    def setUp(self):
        self.item = Item.objects.create(text="Integration test item")

    def test_dumps_with_complex_data(self):
        """Test dumps with complex nested data containing various types."""
        data = {
            "model": self.item,
            "enum": ExampleEnum.ACTIVE,
            "dataclass": ExampleDataclass(name="test", value=42),
            "pydantic": ExamplePydanticModel(title="test", count=5),
            "set": {1, 2, 3},
            "nested": {"list": [1, 2, 3], "string": "test"},
        }

        result = dumps(data)

        self.assertIsInstance(result, str)
        parsed = json.loads(result)

        # Verify serialization results
        self.assertEqual(parsed["model"], str(self.item.pk))
        self.assertEqual(parsed["enum"], "active")
        self.assertEqual(parsed["dataclass"], {"name": "test", "value": 42})
        self.assertEqual(parsed["pydantic"], {"title": "test", "count": 5})
        self.assertIsInstance(parsed["set"], list)
        self.assertEqual(set(parsed["set"]), {1, 2, 3})
        self.assertEqual(parsed["nested"]["list"], [1, 2, 3])
        self.assertEqual(parsed["nested"]["string"], "test")

    def test_encode_decode_roundtrip(self):
        """Test encode/decode roundtrip preserves model data."""
        original_text = self.item.text
        original_pk = self.item.pk

        # Encode then decode
        encoded = encode(self.item)
        decoded = decode(encoded)

        # Verify data is preserved
        self.assertEqual(decoded.pk, original_pk)
        self.assertEqual(decoded.text, original_text)  # type: ignore[attr-defined]

        # Verify the save method is attached
        self.assertTrue(callable(decoded.save))
