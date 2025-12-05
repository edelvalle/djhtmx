from http import HTTPStatus
from unittest.mock import Mock, patch

from django.test import RequestFactory, TestCase
from django.utils.safestring import mark_safe
from fision.todo.htmx import TodoItem  # type: ignore[import-untyped]

from djhtmx.commands import PushURL, ReplaceURL, SendHtml
from djhtmx.component import Destroy, DispatchDOMEvent, Focus, Open, Redirect
from djhtmx.urls import APP_CONFIGS, app_name_of_component, endpoint


class TestEndpoint(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_endpoint_missing_hx_session_header(self):
        """Test endpoint returns 400 when HX-Session header is missing."""
        request = self.factory.post("/test")

        response = endpoint(request, "TestComponent", "test-id", "test_handler")

        self.assertEqual(response.status_code, HTTPStatus.BAD_REQUEST)
        self.assertEqual(response.content.decode(), "Missing header HX-Session")

    @patch("djhtmx.urls.Repository")
    @patch("djhtmx.urls.parse_request_data")
    @patch("djhtmx.urls.tracing_span")
    def test_endpoint_with_hx_session_header(self, mock_span, mock_parse, mock_repo_class):
        """Test endpoint processes request with HX-Session header."""
        request = self.factory.post("/test", data={"key": "value"})
        request.META["HTTP_HX_SESSION"] = "test-session"

        # Mock dependencies
        mock_parse.return_value = {"parsed": "data"}
        mock_repo = Mock()
        mock_repo.dispatch_event.return_value = []
        mock_repo_class.from_request.return_value = mock_repo
        mock_span.return_value.__enter__ = Mock()
        mock_span.return_value.__exit__ = Mock()

        response = endpoint(request, "TestComponent", "test-id", "test_handler")

        self.assertEqual(response.status_code, 200)
        mock_repo.dispatch_event.assert_called_once_with(
            "test-id", "test_handler", {"parsed": "data"}
        )

    @patch("djhtmx.urls.Repository")
    @patch("djhtmx.urls.parse_request_data")
    @patch("djhtmx.urls.tracing_span")
    def test_endpoint_with_hx_prompt_header(self, mock_span, mock_parse, mock_repo_class):
        """Test endpoint includes prompt data when HX-Prompt header present."""
        request = self.factory.post("/test", data={"key": "value"})
        request.META["HTTP_HX_SESSION"] = "test-session"
        request.META["HTTP_HX_PROMPT"] = "user prompt text"

        # Mock dependencies
        mock_parse.return_value = {"parsed": "data"}
        mock_repo = Mock()
        mock_repo.dispatch_event.return_value = []
        mock_repo_class.from_request.return_value = mock_repo
        mock_span.return_value.__enter__ = Mock()
        mock_span.return_value.__exit__ = Mock()

        endpoint(request, "TestComponent", "test-id", "test_handler")

        expected_data = {"parsed": "data", "prompt": "user prompt text"}
        mock_repo.dispatch_event.assert_called_once_with("test-id", "test_handler", expected_data)

    @patch("djhtmx.urls.Repository")
    @patch("djhtmx.urls.parse_request_data")
    @patch("djhtmx.urls.tracing_span")
    def test_endpoint_destroy_command(self, mock_span, mock_parse, mock_repo_class):
        """Test endpoint handles Destroy command."""
        request = self.factory.post("/test")
        request.META["HTTP_HX_SESSION"] = "test-session"

        # Mock dependencies
        mock_parse.return_value = {}
        mock_repo = Mock()
        destroy_command = Destroy("component-123")
        mock_repo.dispatch_event.return_value = [destroy_command]
        mock_repo_class.from_request.return_value = mock_repo
        mock_span.return_value.__enter__ = Mock()
        mock_span.return_value.__exit__ = Mock()

        response = endpoint(request, "TestComponent", "test-id", "test_handler")

        self.assertEqual(response.status_code, 200)
        self.assertIn('hx-swap-oob="delete"', response.content.decode())
        self.assertIn('id="component-123"', response.content.decode())

    @patch("djhtmx.urls.Repository")
    @patch("djhtmx.urls.parse_request_data")
    @patch("djhtmx.urls.tracing_span")
    def test_endpoint_redirect_command(self, mock_span, mock_parse, mock_repo_class):
        """Test endpoint handles Redirect command."""
        request = self.factory.post("/test")
        request.META["HTTP_HX_SESSION"] = "test-session"

        # Mock dependencies
        mock_parse.return_value = {}
        mock_repo = Mock()
        redirect_command = Redirect("/redirect-url")
        mock_repo.dispatch_event.return_value = [redirect_command]
        mock_repo_class.from_request.return_value = mock_repo
        mock_span.return_value.__enter__ = Mock()
        mock_span.return_value.__exit__ = Mock()

        response = endpoint(request, "TestComponent", "test-id", "test_handler")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["HX-Redirect"], "/redirect-url")

    @patch("djhtmx.urls.Repository")
    @patch("djhtmx.urls.parse_request_data")
    @patch("djhtmx.urls.tracing_span")
    def test_endpoint_focus_command(self, mock_span, mock_parse, mock_repo_class):
        """Test endpoint handles Focus command."""
        request = self.factory.post("/test")
        request.META["HTTP_HX_SESSION"] = "test-session"

        # Mock dependencies
        mock_parse.return_value = {}
        mock_repo = Mock()
        focus_command = Focus("#input-field")
        mock_repo.dispatch_event.return_value = [focus_command]
        mock_repo_class.from_request.return_value = mock_repo
        mock_span.return_value.__enter__ = Mock()
        mock_span.return_value.__exit__ = Mock()

        response = endpoint(request, "TestComponent", "test-id", "test_handler")

        self.assertEqual(response.status_code, 200)
        # Focus command should set HX-Trigger-After-Settle header
        self.assertIn("HX-Trigger-After-Settle", response)

    @patch("djhtmx.urls.Repository")
    @patch("djhtmx.urls.parse_request_data")
    @patch("djhtmx.urls.tracing_span")
    def test_endpoint_open_command(self, mock_span, mock_parse, mock_repo_class):
        """Test endpoint handles Open command."""
        request = self.factory.post("/test")
        request.META["HTTP_HX_SESSION"] = "test-session"

        # Mock dependencies
        mock_parse.return_value = {}
        mock_repo = Mock()
        open_command = Open("/open-url", "window_name", "_blank", "noopener")
        mock_repo.dispatch_event.return_value = [open_command]
        mock_repo_class.from_request.return_value = mock_repo
        mock_span.return_value.__enter__ = Mock()
        mock_span.return_value.__exit__ = Mock()

        response = endpoint(request, "TestComponent", "test-id", "test_handler")

        self.assertEqual(response.status_code, 200)
        # Open command should set HX-Trigger-After-Settle header
        self.assertIn("HX-Trigger-After-Settle", response)

    @patch("djhtmx.urls.Repository")
    @patch("djhtmx.urls.parse_request_data")
    @patch("djhtmx.urls.tracing_span")
    def test_endpoint_dispatch_dom_event_command(self, mock_span, mock_parse, mock_repo_class):
        """Test endpoint handles DispatchDOMEvent command."""
        request = self.factory.post("/test")
        request.META["HTTP_HX_SESSION"] = "test-session"

        # Mock dependencies
        mock_parse.return_value = {}
        mock_repo = Mock()
        dom_event_command = DispatchDOMEvent(
            "#target", "custom-event", {"data": "value"}, True, False, True
        )
        mock_repo.dispatch_event.return_value = [dom_event_command]
        mock_repo_class.from_request.return_value = mock_repo
        mock_span.return_value.__enter__ = Mock()
        mock_span.return_value.__exit__ = Mock()

        response = endpoint(request, "TestComponent", "test-id", "test_handler")

        self.assertEqual(response.status_code, 200)
        # DispatchDOMEvent command should set HX-Trigger-After-Settle header
        self.assertIn("HX-Trigger-After-Settle", response)

    @patch("djhtmx.urls.Repository")
    @patch("djhtmx.urls.parse_request_data")
    @patch("djhtmx.urls.tracing_span")
    def test_endpoint_send_html_command(self, mock_span, mock_parse, mock_repo_class):
        """Test endpoint handles SendHtml command."""
        request = self.factory.post("/test")
        request.META["HTTP_HX_SESSION"] = "test-session"

        # Mock dependencies
        mock_parse.return_value = {}
        mock_repo = Mock()
        html_command = SendHtml(mark_safe("<div>Custom HTML</div>"))
        mock_repo.dispatch_event.return_value = [html_command]
        mock_repo_class.from_request.return_value = mock_repo
        mock_span.return_value.__enter__ = Mock()
        mock_span.return_value.__exit__ = Mock()

        response = endpoint(request, "TestComponent", "test-id", "test_handler")

        self.assertEqual(response.status_code, 200)
        self.assertIn("<div>Custom HTML</div>", response.content.decode())

    @patch("djhtmx.urls.Repository")
    @patch("djhtmx.urls.parse_request_data")
    @patch("djhtmx.urls.tracing_span")
    def test_endpoint_push_url_command(self, mock_span, mock_parse, mock_repo_class):
        """Test endpoint handles PushURL command."""
        request = self.factory.post("/test")
        request.META["HTTP_HX_SESSION"] = "test-session"

        # Mock dependencies
        mock_parse.return_value = {}
        mock_repo = Mock()
        push_url_command = PushURL("/new-url")
        mock_repo.dispatch_event.return_value = [push_url_command]
        mock_repo_class.from_request.return_value = mock_repo
        mock_span.return_value.__enter__ = Mock()
        mock_span.return_value.__exit__ = Mock()

        response = endpoint(request, "TestComponent", "test-id", "test_handler")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["HX-Push-Url"], "/new-url")

    @patch("djhtmx.urls.Repository")
    @patch("djhtmx.urls.parse_request_data")
    @patch("djhtmx.urls.tracing_span")
    def test_endpoint_replace_url_command(self, mock_span, mock_parse, mock_repo_class):
        """Test endpoint handles ReplaceURL command."""
        request = self.factory.post("/test")
        request.META["HTTP_HX_SESSION"] = "test-session"

        # Mock dependencies
        mock_parse.return_value = {}
        mock_repo = Mock()
        replace_url_command = ReplaceURL("/replace-url")
        mock_repo.dispatch_event.return_value = [replace_url_command]
        mock_repo_class.from_request.return_value = mock_repo
        mock_span.return_value.__enter__ = Mock()
        mock_span.return_value.__exit__ = Mock()

        response = endpoint(request, "TestComponent", "test-id", "test_handler")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["HX-Replace-Url"], "/replace-url")

    @patch("djhtmx.urls.Repository")
    @patch("djhtmx.urls.parse_request_data")
    @patch("djhtmx.urls.tracing_span")
    def test_endpoint_multiple_commands(self, mock_span, mock_parse, mock_repo_class):
        """Test endpoint handles multiple commands."""
        request = self.factory.post("/test")
        request.META["HTTP_HX_SESSION"] = "test-session"

        # Mock dependencies
        mock_parse.return_value = {}
        mock_repo = Mock()
        commands = [
            SendHtml(mark_safe("<div>First</div>")),
            SendHtml(mark_safe("<div>Second</div>")),
            Redirect("/redirect"),
        ]
        mock_repo.dispatch_event.return_value = commands
        mock_repo_class.from_request.return_value = mock_repo
        mock_span.return_value.__enter__ = Mock()
        mock_span.return_value.__exit__ = Mock()

        response = endpoint(request, "TestComponent", "test-id", "test_handler")

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("<div>First</div>", content)
        self.assertIn("<div>Second</div>", content)
        self.assertEqual(response["HX-Redirect"], "/redirect")


class TestAppNameOfComponent(TestCase):
    def test_app_name_of_component_exact_match(self):
        """Test app_name_of_component with exact app module match."""

        # Create a mock component class
        class MockComponent:
            __module__ = "fision.todo.htmx"

        result = app_name_of_component(MockComponent)

        # Should match the todo app
        self.assertEqual(result, "todo")

    def test_app_name_of_component_partial_match(self):
        """Test app_name_of_component with partial module match."""

        # Create a mock component class
        class MockComponent:
            __module__ = "some.other.module"

        result = app_name_of_component(MockComponent)

        # Should return the full module name when no app matches
        self.assertEqual(result, "some.other.module")

    def test_app_name_of_component_real_component(self):
        """Test app_name_of_component with real component."""
        result = app_name_of_component(TodoItem)

        # Should match the todo app
        self.assertEqual(result, "todo")

    def test_app_configs_sorted_by_length(self):
        """Test APP_CONFIGS is sorted by name length (longest first)."""
        # Verify APP_CONFIGS is sorted correctly
        names = [config.name for config in APP_CONFIGS]
        sorted_names = sorted(names, key=len, reverse=True)

        self.assertEqual(names, sorted_names)
