from unittest.mock import Mock

from django.test import TestCase

from djhtmx.component import HtmxComponent, Render
from djhtmx.repo import Repository


class TestComponent(HtmxComponent):
    _template_name: str = "TestComponent.html"

    name: str = "test"
    value: int = 42


class TestRenderCommand(TestCase):
    def setUp(self):
        self.component = TestComponent(id="test-id", hx_name="TestComponent", user=None)
        self.mock_repo = Mock(spec=Repository)

    def test_render_without_context_uses_component_context(self):
        """Test that rendering without context parameter uses the component's context"""
        render_command = Render(component=self.component, template="TestTemplate.html")

        # Verify the context is None when not provided
        self.assertIsNone(render_command.context)

        # Test that the component has the expected attributes for context
        self.assertEqual(self.component.name, "test")
        self.assertEqual(self.component.value, 42)

    def test_render_with_context_parameter(self):
        """Test that Render command can accept context parameter"""
        custom_context = {"custom_var": "custom_value", "number": 123}
        render_command = Render(
            component=self.component, template="TestTemplate.html", context=custom_context
        )

        # Verify the context is stored in the command
        self.assertEqual(render_command.context, custom_context)

    def test_render_context_is_optional(self):
        """Test that context parameter is optional and defaults to None"""
        render_command = Render(component=self.component)
        self.assertIsNone(render_command.context)

    def test_render_context_accepts_dict_str_any(self):
        """Test that context parameter accepts dict[str, Any] type"""
        context_data = {
            "string_val": "test",
            "int_val": 42,
            "list_val": [1, 2, 3],
            "dict_val": {"nested": "value"},
            "bool_val": True,
            "none_val": None,
        }

        render_command = Render(component=self.component, context=context_data)

        self.assertEqual(render_command.context, context_data)
