from unittest.mock import Mock

from django.template import Context
from django.test import TestCase

from djhtmx.component import HtmxComponent
from djhtmx.repo import Repository
from djhtmx.templatetags.htmx import htmx


class TestComponent(HtmxComponent):
    """Test component for testing."""

    _template_name = "TestComponent.html"

    name: str = "test"
    value: int = 42


class AnotherComponent(HtmxComponent):
    """Another test component."""

    _template_name = "AnotherComponent.html"

    data: str = "example"


class TestHtmxTemplateTag(TestCase):
    """Test the htmx template tag."""

    def setUp(self):
        """Set up test fixtures."""
        self.mock_repo = Mock(spec=Repository)
        self.mock_component = Mock(spec=HtmxComponent)
        self.mock_component.id = "test-id"

        # Mock the build method to return our mock component
        self.mock_repo.build.return_value = self.mock_component

        # Mock the render_html method
        self.mock_repo.render_html.return_value = "<div>rendered</div>"

        # Create a context with the repository
        self.context = Context({"htmx_repo": self.mock_repo})

    def test_htmx_with_string_name(self):
        """Test that htmx tag works with string component name."""
        result = htmx(self.context, "TestComponent", {"key": "value"})

        # Verify build was called with the string name
        self.mock_repo.build.assert_called_once()
        call_args = self.mock_repo.build.call_args
        self.assertEqual(call_args[0][0], "TestComponent")

        # Verify render_html was called
        self.mock_repo.render_html.assert_called_once()
        self.assertEqual(result, "<div>rendered</div>")

    def test_htmx_with_component_type(self):
        """Test that htmx tag extracts name from component type."""
        result = htmx(self.context, TestComponent, {"key": "value"})

        # Verify build was called with the extracted component name
        self.mock_repo.build.assert_called_once()
        call_args = self.mock_repo.build.call_args
        self.assertEqual(call_args[0][0], "TestComponent")

        # Verify render_html was called
        self.mock_repo.render_html.assert_called_once()
        self.assertEqual(result, "<div>rendered</div>")

    def test_htmx_with_different_component_type(self):
        """Test that htmx tag works with different component types."""
        result = htmx(self.context, AnotherComponent, {"key": "value"})

        # Verify build was called with the correct component name
        self.mock_repo.build.assert_called_once()
        call_args = self.mock_repo.build.call_args
        self.assertEqual(call_args[0][0], "AnotherComponent")

        # Verify render_html was called
        self.mock_repo.render_html.assert_called_once()
        self.assertEqual(result, "<div>rendered</div>")

    def test_htmx_with_kwargs_state(self):
        """Test that htmx tag passes state from kwargs."""
        htmx(self.context, TestComponent, key1="value1", key2="value2")

        # Verify build was called with the state including lazy
        self.mock_repo.build.assert_called_once()
        call_args = self.mock_repo.build.call_args
        state = call_args[0][1]
        self.assertIn("key1", state)
        self.assertIn("key2", state)
        self.assertEqual(state["key1"], "value1")
        self.assertEqual(state["key2"], "value2")
        self.assertIn("lazy", state)
        self.assertEqual(state["lazy"], False)

    def test_htmx_with_dict_and_kwargs_state(self):
        """Test that htmx tag merges dict and kwargs state."""
        htmx(
            self.context,
            TestComponent,
            {"dict_key": "dict_value"},
            kwarg_key="kwarg_value",
        )

        # Verify build was called with merged state
        self.mock_repo.build.assert_called_once()
        call_args = self.mock_repo.build.call_args
        state = call_args[0][1]
        self.assertIn("dict_key", state)
        self.assertIn("kwarg_key", state)
        self.assertEqual(state["dict_key"], "dict_value")
        self.assertEqual(state["kwarg_key"], "kwarg_value")

    def test_htmx_with_lazy_true(self):
        """Test that htmx tag handles lazy=True parameter."""
        htmx(self.context, TestComponent, lazy=True)

        # Verify build was called with lazy state
        self.mock_repo.build.assert_called_once()
        call_args = self.mock_repo.build.call_args
        state = call_args[0][1]
        self.assertTrue(state["lazy"])

        # Verify render_html was called with lazy=True
        self.mock_repo.render_html.assert_called_once()
        render_call_args = self.mock_repo.render_html.call_args
        self.assertTrue(render_call_args[1]["lazy"])

    def test_htmx_with_parent_context(self):
        """Test that htmx tag extracts parent_id from context."""
        parent_component = Mock()
        parent_component.id = "parent-123"
        context_with_parent = Context({
            "htmx_repo": self.mock_repo,
            "this": parent_component,
        })

        htmx(context_with_parent, TestComponent)

        # Verify build was called with parent_id
        self.mock_repo.build.assert_called_once()
        call_args = self.mock_repo.build.call_args
        self.assertEqual(call_args[1]["parent_id"], "parent-123")
