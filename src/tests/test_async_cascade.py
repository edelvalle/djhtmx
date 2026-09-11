"""Sync and async event handlers interoperate through the command/event bus.

Handlers never call each other directly: one handler emits an event, and the
pipeline wakes the listening component's `_handle_event`.  `_invoke_handler`
adapts each handler to its own color at its own dispatch point (sync handlers
run directly, async handlers via async_to_sync), so a sync handler can wake an
async listener and vice-versa.
"""

from dataclasses import dataclass

from django.http import QueryDict
from django.test import TestCase

from djhtmx.commands import Emit, SkipRender
from djhtmx.component import HtmxComponent
from djhtmx.repo import Repository, Session

CAUGHT: list[tuple[str, str]] = []


@dataclass
class Ping:
    pass


@dataclass
class Pong:
    pass


class CascadeSyncEmitter(HtmxComponent):
    _template_name = "CascadeSyncEmitter.html"

    def fire(self):
        return [Emit(Ping()), SkipRender(self)]


class CascadeAsyncListener(HtmxComponent):
    _template_name = "CascadeAsyncListener.html"

    async def _handle_event(self, event: Ping):
        CAUGHT.append(("async-listener", type(event).__name__))
        return [SkipRender(self)]


class CascadeAsyncEmitter(HtmxComponent):
    _template_name = "CascadeAsyncEmitter.html"

    async def fire(self):
        return [Emit(Pong()), SkipRender(self)]


class CascadeSyncListener(HtmxComponent):
    _template_name = "CascadeSyncListener.html"

    def _handle_event(self, event: Pong):
        CAUGHT.append(("sync-listener", type(event).__name__))
        return [SkipRender(self)]


class AsyncCascadeTest(TestCase):
    def _dispatch(self, emitter: HtmxComponent, listener: HtmxComponent):
        CAUGHT.clear()
        session = Session(Repository.new_session_id())
        repo = Repository(user=None, session=session, params=QueryDict())
        for component in (emitter, listener):
            session.states[component.id] = component.model_dump_json()
        session.read = True  # in-memory state; skip the redis read

        return list(repo.dispatch_event(emitter.id, "fire", {}))

    def test_sync_handler_wakes_async_listener(self):
        emitter = CascadeSyncEmitter(hx_name="CascadeSyncEmitter", id="em", user=None)
        listener = CascadeAsyncListener(hx_name="CascadeAsyncListener", id="li", user=None)
        self._dispatch(emitter, listener)
        self.assertEqual(CAUGHT, [("async-listener", "Ping")])

    def test_async_handler_wakes_sync_listener(self):
        emitter = CascadeAsyncEmitter(hx_name="CascadeAsyncEmitter", id="em", user=None)
        listener = CascadeSyncListener(hx_name="CascadeSyncListener", id="li", user=None)
        self._dispatch(emitter, listener)
        self.assertEqual(CAUGHT, [("sync-listener", "Pong")])
