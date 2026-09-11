"""The async endpoint survives Django's refusal to wrap async views.

`ATOMIC_REQUESTS` is honoured by djhtmx itself: `Repository.atomic_dispatch`
opens one transaction around the whole dispatch, on the pool thread that owns
the connection.  Django must therefore be told not to try wrapping the view,
for *every* database and not only the default one.
"""

from django.core.handlers import base as base_handler
from django.test import TestCase

from djhtmx.urls import _make_endpoint_view  # noqa: PLC2701  (white-box test)


class EndpointNonAtomicTest(TestCase):
    """The async endpoint view must survive Django's `make_view_atomic` even
    when a *non-default* database has ATOMIC_REQUESTS (it opts out of all DBs;
    djhtmx opens the dispatch transaction itself)."""

    def test_endpoint_view_opts_out_of_every_atomic_database(self):
        from django.db import connections

        connections.settings["default"]["ATOMIC_REQUESTS"] = True
        connections.settings["analytics"] = {
            **connections.settings["default"],
            "ATOMIC_REQUESTS": True,
        }
        self.addCleanup(lambda: connections.settings.pop("analytics", None))
        self.addCleanup(
            lambda: connections.settings["default"].__setitem__("ATOMIC_REQUESTS", False)
        )

        view = _make_endpoint_view("TodoList")
        self.assertIn("default", view._non_atomic_requests)
        self.assertIn("analytics", view._non_atomic_requests)

        # Must not raise "You cannot use ATOMIC_REQUESTS with async views."
        base_handler.BaseHandler().make_view_atomic(view)
