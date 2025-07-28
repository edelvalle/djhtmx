
from django.http import HttpRequest, QueryDict
from django.test import TestCase
from fision.todo.models import Item  # type: ignore[import-untyped]

from djhtmx.utils import (
    compact_hash,
    generate_id,
    get_instance_subscriptions,
    get_model_subscriptions,
    get_params,
)


class TestGetParams(TestCase):
    def test_http_request_htmx_request(self):
        """Test get_params with HTMX request having HX_CURRENT_URL."""
        request = HttpRequest()
        request.META = {
            "HTTP_HX_REQUEST": "true",
            "HTTP_HX_CURRENT_URL": "http://example.com/path?param1=value1&param2=value2",
        }

        result = get_params(request)

        self.assertIsInstance(result, QueryDict)
        self.assertTrue(result._mutable)
        self.assertEqual(result.get("param1"), "value1")
        self.assertEqual(result.get("param2"), "value2")

    def test_http_request_non_htmx_request(self):
        """Test get_params with regular HTTP request."""
        request = HttpRequest()
        request.META = {"HTTP_HX_REQUEST": "false"}
        request.GET = QueryDict("get_param=get_value")

        result = get_params(request)

        self.assertIsInstance(result, QueryDict)
        self.assertTrue(result._mutable)
        self.assertEqual(result.get("get_param"), "get_value")

    def test_http_request_no_hx_request_header(self):
        """Test get_params with request missing HX_REQUEST header."""
        request = HttpRequest()
        request.META = {}
        request.GET = QueryDict("param=value")

        result = get_params(request)

        self.assertIsInstance(result, QueryDict)
        self.assertTrue(result._mutable)
        self.assertEqual(result.get("param"), "value")

    def test_querydict_input(self):
        """Test get_params with QueryDict input."""
        query_dict = QueryDict("param1=value1&param2=value2")

        result = get_params(query_dict)

        self.assertIsInstance(result, QueryDict)
        self.assertTrue(result._mutable)
        self.assertEqual(result.get("param1"), "value1")
        self.assertEqual(result.get("param2"), "value2")

    def test_string_input_with_query(self):
        """Test get_params with string URL input."""
        url_string = "http://example.com/path?param1=value1&param2=value2"

        result = get_params(url_string)

        self.assertIsInstance(result, QueryDict)
        self.assertTrue(result._mutable)
        self.assertEqual(result.get("param1"), "value1")
        self.assertEqual(result.get("param2"), "value2")

    def test_string_input_without_query(self):
        """Test get_params with string URL without query parameters."""
        url_string = "http://example.com/path"

        result = get_params(url_string)

        self.assertIsInstance(result, QueryDict)
        self.assertTrue(result._mutable)
        self.assertEqual(len(result), 0)

    def test_empty_string_input(self):
        """Test get_params with empty string."""
        result = get_params("")

        self.assertIsInstance(result, QueryDict)
        self.assertTrue(result._mutable)
        self.assertEqual(len(result), 0)

    def test_none_input(self):
        """Test get_params with None input."""
        result = get_params(None)

        self.assertIsInstance(result, QueryDict)
        self.assertTrue(result._mutable)
        self.assertEqual(len(result), 0)


class TestSubscriptions(TestCase):
    def setUp(self):
        self.item = Item.objects.create(text="Test item")

    def test_get_instance_subscriptions_default_actions(self):
        """Test get_instance_subscriptions with default actions."""
        result = get_instance_subscriptions(self.item)

        expected = {
            f"todo.item.{self.item.pk}.created",
            f"todo.item.{self.item.pk}.updated",
            f"todo.item.{self.item.pk}.deleted",
        }
        self.assertEqual(result, expected)

    def test_get_instance_subscriptions_custom_actions(self):
        """Test get_instance_subscriptions with custom actions."""
        actions = ["activated", "deactivated"]
        result = get_instance_subscriptions(self.item, actions)

        expected = {
            f"todo.item.{self.item.pk}.activated",
            f"todo.item.{self.item.pk}.deactivated",
        }
        self.assertEqual(result, expected)

    def test_get_instance_subscriptions_no_actions(self):
        """Test get_instance_subscriptions with empty actions."""
        result = get_instance_subscriptions(self.item, [])

        expected = {f"todo.item.{self.item.pk}"}
        self.assertEqual(result, expected)

    def test_get_model_subscriptions_with_instance(self):
        """Test get_model_subscriptions with model instance."""
        result = get_model_subscriptions(self.item)

        expected = {
            "todo.item",
            f"todo.item.{self.item.pk}",
            f"todo.item.{self.item.pk}.created",
            f"todo.item.{self.item.pk}.updated",
            f"todo.item.{self.item.pk}.deleted",
        }
        self.assertEqual(result, expected)

    def test_get_model_subscriptions_with_class(self):
        """Test get_model_subscriptions with model class."""
        result = get_model_subscriptions(Item)

        expected = {"todo.item"}
        self.assertEqual(result, expected)

    def test_get_model_subscriptions_custom_actions(self):
        """Test get_model_subscriptions with custom actions."""
        actions = ["published", "archived"]
        result = get_model_subscriptions(self.item, actions)

        expected = {
            "todo.item",
            f"todo.item.{self.item.pk}",
            f"todo.item.{self.item.pk}.published",
            f"todo.item.{self.item.pk}.archived",
        }
        self.assertEqual(result, expected)


class TestUtilityFunctions(TestCase):
    def test_generate_id_format(self):
        """Test generate_id returns correctly formatted ID."""
        result = generate_id()

        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("hx-"))
        # UUID7 hex should be 32 characters
        self.assertEqual(len(result), 35)  # "hx-" + 32 hex chars

    def test_generate_id_unique(self):
        """Test generate_id returns unique values."""
        ids = {generate_id() for _ in range(100)}

        # All IDs should be unique
        self.assertEqual(len(ids), 100)

    def test_compact_hash_consistent(self):
        """Test compact_hash returns consistent results for same input."""
        value = "test_string"

        result1 = compact_hash(value)
        result2 = compact_hash(value)

        self.assertEqual(result1, result2)

    def test_compact_hash_different_inputs(self):
        """Test compact_hash returns different results for different inputs."""
        value1 = "test_string_1"
        value2 = "test_string_2"

        result1 = compact_hash(value1)
        result2 = compact_hash(value2)

        self.assertNotEqual(result1, result2)

    def test_compact_hash_non_empty(self):
        """Test compact_hash returns non-empty string."""
        result = compact_hash("test")

        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 0)

    def test_compact_hash_safe_characters(self):
        """Test compact_hash uses URL/CSS safe characters."""
        result = compact_hash("test_value")

        # All characters should be from the _BASE string
        base_chars = "ZmBeUHhTgusXNW_Y1b05KPiFcQJD86joqnIRE7Lfkrdp3AOMCvltSwzVG9yxa42"
        for char in result:
            self.assertIn(char, base_chars)
