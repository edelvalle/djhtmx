"""Smoke test: the synchronous dispatcher runs sync handlers directly and
wraps async / async-generator handlers via async_to_sync.  Handlers return
SkipRender so the test needs no template on disk."""

from django.http import QueryDict
from django.test import TestCase

from djhtmx.commands import SkipRender
from djhtmx.component import HtmxComponent
from djhtmx.repo import Repository, Session

CALLS: list[str] = []


class _Probe(HtmxComponent):
    _template_name = "_Probe.html"

    def sync_handler(self):
        CALLS.append("sync")
        return [SkipRender(self)]

    async def async_handler(self):
        CALLS.append("async")
        return [SkipRender(self)]

    async def asyncgen_handler(self):
        CALLS.append("asyncgen")
        yield SkipRender(self)


class AsyncDispatchTest(TestCase):
    def _dispatch(self, handler_name):
        CALLS.clear()
        session = Session(Repository.new_session_id())
        repo = Repository(user=None, session=session, params=QueryDict())
        comp = _Probe(hx_name="_Probe", id="probe-1", user=None)
        session.states[comp.id] = comp.model_dump_json()
        session.read = True

        return list(repo.dispatch_event(comp.id, handler_name, {}))

    def test_sync_handler_autowrapped(self):
        self._dispatch("sync_handler")
        self.assertEqual(CALLS, ["sync"])

    def test_async_handler_awaited(self):
        self._dispatch("async_handler")
        self.assertEqual(CALLS, ["async"])

    def test_asyncgen_handler(self):
        self._dispatch("asyncgen_handler")
        self.assertEqual(CALLS, ["asyncgen"])
