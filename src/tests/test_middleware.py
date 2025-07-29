import asyncio
from typing import TYPE_CHECKING
from unittest.mock import Mock

from django.http import HttpRequest, HttpResponse
from django.test import TestCase

from djhtmx.middleware import middleware

if TYPE_CHECKING:
    pass


class TestMiddleware(TestCase):
    def test_sync_middleware_creation(self):
        """Test middleware creation with sync get_response."""

        def sync_get_response(request):
            return HttpResponse("OK")

        middleware_func = middleware(sync_get_response)

        self.assertIsNotNone(middleware_func)
        self.assertFalse(asyncio.iscoroutinefunction(middleware_func))

    def test_async_middleware_creation(self):
        """Test middleware creation with async get_response."""

        async def async_get_response(request):  # type: ignore[misc]
            return HttpResponse("OK")

        middleware_func = middleware(async_get_response)

        self.assertIsNotNone(middleware_func)
        self.assertTrue(asyncio.iscoroutinefunction(middleware_func))

    def test_sync_middleware_without_repo(self):
        """Test sync middleware when request has no htmx_repo."""

        def sync_get_response(request):
            return HttpResponse("OK")

        middleware_func = middleware(sync_get_response)
        request = HttpRequest()

        response = middleware_func(request)

        self.assertIsInstance(response, HttpResponse)
        self.assertEqual(response.content, b"OK")  # type: ignore[attr-defined]

    def test_sync_middleware_with_repo(self):
        """Test sync middleware when request has htmx_repo."""

        def sync_get_response(request):
            return HttpResponse("OK")

        middleware_func = middleware(sync_get_response)
        request = HttpRequest()

        # Mock repository with session
        mock_session = Mock()
        mock_repo = Mock()
        mock_repo.session = mock_session
        request.htmx_repo = mock_repo  # type: ignore[attr-defined]

        response = middleware_func(request)

        # Verify response
        self.assertIsInstance(response, HttpResponse)
        self.assertEqual(response.content, b"OK")  # type: ignore[attr-defined]

        # Verify session was flushed and repo was removed
        mock_session.flush.assert_called_once()
        self.assertFalse(hasattr(request, "htmx_repo"))

    async def test_async_middleware_without_repo(self):
        """Test async middleware when request has no htmx_repo."""

        async def async_get_response(request):  # type: ignore[misc]
            return HttpResponse("OK")

        middleware_func = middleware(async_get_response)
        request = HttpRequest()

        response = await middleware_func(request)  # type: ignore[misc]  # type: ignore[misc]

        self.assertIsInstance(response, HttpResponse)
        self.assertEqual(response.content, b"OK")  # type: ignore[attr-defined]

    async def test_async_middleware_with_repo(self):
        """Test async middleware when request has htmx_repo."""

        async def async_get_response(request):  # type: ignore[misc]
            return HttpResponse("OK")

        middleware_func = middleware(async_get_response)
        request = HttpRequest()

        # Mock repository with session
        mock_session = Mock()
        mock_repo = Mock()
        mock_repo.session = mock_session
        request.htmx_repo = mock_repo  # type: ignore[attr-defined]

        response = await middleware_func(request)  # type: ignore[misc]

        # Verify response
        self.assertIsInstance(response, HttpResponse)
        self.assertEqual(response.content, b"OK")  # type: ignore[attr-defined]

        # Verify session was flushed and repo was removed
        mock_session.flush.assert_called_once()
        self.assertFalse(hasattr(request, "htmx_repo"))

    def test_sync_middleware_preserves_response(self):
        """Test sync middleware preserves the original response."""
        expected_content = "Custom Response Content"
        expected_status = 201

        def sync_get_response(request):
            return HttpResponse(expected_content, status=expected_status)

        middleware_func = middleware(sync_get_response)
        request = HttpRequest()

        response = middleware_func(request)

        self.assertEqual(response.content.decode(), expected_content)  # type: ignore[attr-defined]
        self.assertEqual(response.status_code, expected_status)  # type: ignore[attr-defined]

    async def test_async_middleware_preserves_response(self):
        """Test async middleware preserves the original response."""
        expected_content = "Custom Async Response"
        expected_status = 202

        async def async_get_response(request):  # type: ignore[misc]
            return HttpResponse(expected_content, status=expected_status)

        middleware_func = middleware(async_get_response)
        request = HttpRequest()

        response = await middleware_func(request)  # type: ignore[misc]

        self.assertEqual(response.content.decode(), expected_content)  # type: ignore[attr-defined]
        self.assertEqual(response.status_code, expected_status)  # type: ignore[attr-defined]

    def test_sync_middleware_exception_in_cleanup_propagates(self):
        """Test sync middleware propagates exceptions during cleanup."""

        def sync_get_response(request):
            return HttpResponse("OK")

        middleware_func = middleware(sync_get_response)
        request = HttpRequest()

        # Mock repository with session that raises exception
        mock_session = Mock()
        mock_session.flush.side_effect = Exception("Flush failed")
        mock_repo = Mock()
        mock_repo.session = mock_session
        request.htmx_repo = mock_repo  # type: ignore[attr-defined]

        # Exception should propagate
        with self.assertRaises(Exception) as cm:
            middleware_func(request)

        self.assertEqual(str(cm.exception), "Flush failed")
        mock_session.flush.assert_called_once()

    async def test_async_middleware_exception_in_cleanup_propagates(self):
        """Test async middleware propagates exceptions during cleanup."""

        async def async_get_response(request):  # type: ignore[misc]
            return HttpResponse("OK")

        middleware_func = middleware(async_get_response)
        request = HttpRequest()

        # Mock repository with session that raises exception
        mock_session = Mock()
        mock_session.flush.side_effect = Exception("Async flush failed")
        mock_repo = Mock()
        mock_repo.session = mock_session
        request.htmx_repo = mock_repo  # type: ignore[attr-defined]

        # Exception should propagate
        with self.assertRaises(Exception) as cm:
            await middleware_func(request)

        self.assertEqual(str(cm.exception), "Async flush failed")
        mock_session.flush.assert_called_once()

    def test_middleware_function_detection(self):
        """Test middleware correctly detects sync vs async functions."""

        def sync_func(request):
            return HttpResponse()

        async def async_func(request):  # type: ignore[misc]
            return HttpResponse()

        sync_middleware = middleware(sync_func)
        async_middleware = middleware(async_func)  # type: ignore[misc]

        self.assertFalse(asyncio.iscoroutinefunction(sync_middleware))
        self.assertTrue(asyncio.iscoroutinefunction(async_middleware))
