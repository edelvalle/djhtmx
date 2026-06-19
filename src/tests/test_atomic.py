"""Sync event handlers honour ATOMIC_REQUESTS.

Async views bypass Django's per-request atomic wrapping, so the dispatcher wraps
each sync handler in `transaction.atomic` for every database configured with
`ATOMIC_REQUESTS` (and leaves it in autocommit otherwise).
"""

from unittest.mock import MagicMock, patch

from django.core.handlers import base as base_handler
from django.test import TestCase

from djhtmx import command_processor
from djhtmx.urls import _make_endpoint_view  # noqa: PLC2701  (white-box test)


def _fake_connections(atomic_by_alias: dict[str, bool]) -> MagicMock:
    conns = MagicMock()
    conns.__iter__.return_value = iter(list(atomic_by_alias))

    def getitem(alias: str) -> MagicMock:
        c = MagicMock()
        c.settings_dict = {"ATOMIC_REQUESTS": atomic_by_alias[alias]}
        return c

    conns.__getitem__.side_effect = getitem
    return conns


class AtomicHandlerTest(TestCase):
    def _run(self, atomic_by_alias):
        calls = []
        with (
            patch.object(command_processor, "connections", _fake_connections(atomic_by_alias)),
            patch.object(command_processor, "transaction") as tx,
        ):
            tx.atomic.return_value.__exit__.return_value = False
            result = command_processor.CommandProcessor._drain_sync_handler(
                lambda: calls.append("ran") or [], (), {}
            )
        return calls, result, tx

    def test_wraps_when_atomic_requests(self):
        calls, result, tx = self._run({"default": True})
        tx.atomic.assert_called_once_with(using="default")
        self.assertEqual(calls, ["ran"])
        self.assertEqual(result, [])

    def test_no_wrap_without_atomic_requests(self):
        calls, _result, tx = self._run({"default": False})
        tx.atomic.assert_not_called()
        self.assertEqual(calls, ["ran"])

    def test_wraps_each_atomic_database(self):
        _calls, _result, tx = self._run({"default": True, "replica": False, "ledger": True})
        used = sorted(call.kwargs["using"] for call in tx.atomic.call_args_list)
        self.assertEqual(used, ["default", "ledger"])


class EndpointNonAtomicTest(TestCase):
    """The async endpoint view must survive Django's `make_view_atomic` even
    when a *non-default* database has ATOMIC_REQUESTS (it opts out of all DBs;
    atomicity is applied per sync handler instead)."""

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
