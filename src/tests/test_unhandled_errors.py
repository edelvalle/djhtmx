"""A failing handler reaches the application as `HtmxUnhandledError`, whichever shape it has.

A handler that yields its commands has run none of its body by the time it returns: the body runs
when something drains the generator, and every dispatch path drains it *after* the guard that turns
a handler's error into `Emit(HtmxUnhandledError(...))`.  The error therefore escapes the processor,
and no layer up to the view catches it: the application's recovery handler never hears about the
failure and the interaction answers with a server error instead.

These tests pin the promise the guard makes at each of the three places a handler is called -- the
handler an htmx request names, an `_handle_event` woken by an emitted event, and `_handle_sse_events`
-- for both handler shapes.  The returning shape is the control: it is what the yielding shape has
to agree with.

See https://github.com/edelvalle/djhtmx/issues/66.

"""

from collections.abc import Callable
from dataclasses import dataclass

from django.contrib.auth.models import AnonymousUser
from django.test import Client, SimpleTestCase

from djhtmx.command_processor import CommandProcessor
from djhtmx.command_queue import CommandQueue
from djhtmx.commands import Command, Emit, Execute, HandleSSEEvents, InternalCommand, SkipRender
from djhtmx.component import HtmxComponent, annotated_handler
from djhtmx.global_events import HtmxUnhandledError
from djhtmx.repo import Repository, Session
from djhtmx.sse import SSEEventEnvelope, SSESubscription
from djhtmx.testing import Htmx
from djhtmx.utils import get_params


class ProbeError(Exception):
    """What every probe raises, so the reported error can be told apart from an accident."""


@dataclass
class ProbeEvent:
    """The event that wakes the `_handle_event` probes."""


@dataclass
class ProbeSSEEvent:
    """The payload the SSE probes subscribe to."""


PROBE_TOPIC = "djhtmx.tests.probe"


class ErrorProbe(HtmxComponent):
    """The same failure written in each shape an htmx event handler can take.

    `yields_after_working` is the shape the issue singles out: one that does its work and reports
    through what it yields.  What becomes of the commands it emitted before failing is deliberately
    left unasserted -- that is a semantic to decide, not one these tests already know.

    """

    _template_name = "ErrorProbe.html"

    @annotated_handler(probe="returns")
    def returns(self):
        raise ProbeError("boom")

    @annotated_handler(probe="yields")
    def yields(self):
        raise ProbeError("boom")
        yield

    @annotated_handler(probe="yields_after_working")
    def yields_after_working(self):
        yield SkipRender(self)
        raise ProbeError("boom")


class ReturningEventProbe(HtmxComponent):
    """A listener whose `_handle_event` returns its commands."""

    _template_name = "ReturningEventProbe.html"

    def _handle_event(self, event: ProbeEvent):
        raise ProbeError("boom")


class YieldingEventProbe(HtmxComponent):
    """The same listener, yielding its commands."""

    _template_name = "YieldingEventProbe.html"

    def _handle_event(self, event: ProbeEvent):
        raise ProbeError("boom")
        yield


class ReturningSSEProbe(HtmxComponent):
    """An SSE consumer whose handler returns its commands."""

    _template_name = "ReturningSSEProbe.html"

    @property
    def sse_subscriptions(self) -> set[SSESubscription]:
        return {SSESubscription(ProbeSSEEvent, PROBE_TOPIC)}

    def _handle_sse_events(self, envelope: SSEEventEnvelope[ProbeSSEEvent]):
        raise ProbeError("boom")


class YieldingSSEProbe(HtmxComponent):
    """The same consumer, yielding its commands."""

    _template_name = "YieldingSSEProbe.html"

    @property
    def sse_subscriptions(self) -> set[SSESubscription]:
        return {SSESubscription(ProbeSSEEvent, PROBE_TOPIC)}

    def _handle_sse_events(self, envelope: SSEEventEnvelope[ProbeSSEEvent]):
        raise ProbeError("boom")
        yield


class ProbeTestCase(SimpleTestCase):
    """Watch a dispatch with `Htmx.assertEmits`:meth: without a page to drive it from.

    The probes are mounted straight into a session and their commands are run one at a time, which
    is what keeps them off templates they do not have -- so there is nothing for `Htmx` to navigate
    to.  Its assertions do not need one: they watch the processor itself, wherever the dispatch that
    reaches it came from.

    """

    def setUp(self):
        super().setUp()
        self.htmx = Htmx(Client())


class TestHtmxEventHandler(ProbeTestCase):
    """The `Execute` path: the handler an htmx request names."""

    def setUp(self):
        super().setUp()
        self.session_id = mount(ErrorProbe, "probe")

    def test_a_returning_handler_reports_its_error(self):
        """The control: this is the behaviour the yielding shape has to match."""
        with self.htmx.assertEmits(HtmxUnhandledError) as captured:
            self.dispatch(ErrorProbe.returns)

        [reported] = captured
        self.assertIsInstance(reported.error, ProbeError)
        self.assertEqual(reported.handler_annotations, {"probe": "returns"})

    def test_a_yielding_handler_reports_its_error(self):
        with self.htmx.assertEmits(HtmxUnhandledError) as captured:
            self.dispatch(ErrorProbe.yields)

        [reported] = captured
        self.assertIsInstance(reported.error, ProbeError)
        self.assertEqual(reported.handler_annotations, {"probe": "yields"})

    def test_a_yielding_handler_reports_an_error_raised_after_it_emitted(self):
        with self.htmx.assertEmits(HtmxUnhandledError) as captured:
            self.dispatch(ErrorProbe.yields_after_working)

        [reported] = captured
        self.assertIsInstance(reported.error, ProbeError)
        self.assertEqual(reported.handler_annotations, {"probe": "yields_after_working"})

    def dispatch(self, handler: Callable):
        run(self.session_id, Execute("probe", handler.__name__, {}))


class TestEventListener(ProbeTestCase):
    """The `Emit` path: a component woken by an event some other handler emitted."""

    def test_a_returning_event_handler_reports_its_error(self):
        session_id = mount(ReturningEventProbe, "listener")

        with self.htmx.assertEmits(HtmxUnhandledError) as captured:
            run(session_id, Emit(ProbeEvent()))

        [reported] = captured
        self.assertIsInstance(reported.error, ProbeError)

    def test_a_yielding_event_handler_reports_its_error(self):
        session_id = mount(YieldingEventProbe, "listener")

        with self.htmx.assertEmits(HtmxUnhandledError) as captured:
            run(session_id, Emit(ProbeEvent()))

        [reported] = captured
        self.assertIsInstance(reported.error, ProbeError)


class TestSSEEventHandler(ProbeTestCase):
    """The `HandleSSEEvents` path, where an escaping error drops the session's SSE connection."""

    def test_a_returning_sse_handler_reports_its_error(self):
        session_id = mount(ReturningSSEProbe, "consumer")

        with self.htmx.assertEmits(HtmxUnhandledError) as captured:
            run(session_id, build_handle_sse_events("consumer"))

        [reported] = captured
        self.assertIsInstance(reported.error, ProbeError)

    def test_a_yielding_sse_handler_reports_its_error(self):
        session_id = mount(YieldingSSEProbe, "consumer")

        with self.htmx.assertEmits(HtmxUnhandledError) as captured:
            run(session_id, build_handle_sse_events("consumer"))

        [reported] = captured
        self.assertIsInstance(reported.error, ProbeError)


def mount(component_type: type[HtmxComponent], component_id: str) -> str:
    """Put a component of `component_type` in a fresh session, and answer that session's id.

    A dispatch reaches a component only through the session: without its state djhtmx answers with
    a `Destroy` for an unknown component and never builds anything.

    """
    session = Session(Repository.new_session_id())
    repository = Repository(user=AnonymousUser(), session=session, params=get_params(None))
    session.store(repository.build(component_type.__name__, {"id": component_id}))
    session.flush()
    return session.id


def run(session_id: str, command: Command | InternalCommand) -> None:
    """Run `command` against `session_id`.

    Only that one command runs; whatever it queues stays queued, which keeps the renders the
    recovery queues away from templates these components do not have.

    """
    repository = Repository(
        user=AnonymousUser(), session=Session(session_id), params=get_params(None)
    )
    list(CommandProcessor(repository)._run_command(CommandQueue([command])))


def build_handle_sse_events(component_id: str) -> HandleSSEEvents:
    return HandleSSEEvents(
        component_id=component_id,
        envelopes=(SSEEventEnvelope(event=ProbeSSEEvent(), topic=PROBE_TOPIC),),
    )
