"""Command processor for djhtmx.

Owns the command loop that drives an event from its initial `Execute` (or
other root command) through to the stream of `ProcessedCommand` values that
the transport layer (HTTP endpoint, SSE renderer) turns into wire output.

This module is the single source of truth for command semantics.
`Repository` keeps responsibility for component lifecycle, session storage,
template rendering, and query parameter patching — i.e. the *state* a
command consults — but the *decisions* about what each command means live
here.

The pipeline is **synchronous**: the whole dispatch runs as one job on the
bounded sync-work pool (see `sse_executor`), so every database touch happens
on a pool thread that owns a single reused connection.  Postgres connection
count is therefore bounded by `DJHTMX_SYNC_WORKERS`, independent of request or
SSE-stream concurrency.  Components may still define `async def` handlers; they
are run via `async_to_sync` on the pool thread (see `_invoke_handler`), so
their ORM work stays on the same bounded connection.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterable, Awaitable, Callable, Generator, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, assert_never, cast

from asgiref.sync import async_to_sync

from djhtmx.global_events import HtmxUnhandledError
from djhtmx.tracing import tracing_span

from .command_queue import CommandQueue
from .commands import (
    BuildAndRender,
    Command,
    Destroy,
    DispatchDOMEvent,
    Emit,
    Execute,
    Focus,
    HandleSSEEvents,
    InternalCommand,
    InvalidateModelCache,
    Open,
    ProcessedCommand,
    PushURL,
    Redirect,
    Render,
    ReplaceURL,
    ScrollIntoView,
    SendHtml,
    Signal,
    SkipRender,
)
from .component import (
    LISTENERS,
    REGISTRY,
    HandlerKind,
    HtmxComponent,
    get_handler_kind,
)
from .exceptions import LoginRequired
from .introspection import filter_parameters
from .settings import LOGIN_URL
from .utils import atomic_if_requested

if TYPE_CHECKING:
    from .repo import Repository

logger = logging.getLogger(__name__)


class CommandProcessor:
    """Run djhtmx commands against a `Repository` and yield processed commands.

    Instantiated per command run.  The processor is stateless beyond its repository reference; all
    session and component state lives on the `Repository`.

    """

    def __init__(self, repo: Repository):
        self.repo = repo

    def process(self, commands: Iterable[Command | InternalCommand]) -> Generator[ProcessedCommand]:
        """Drive the command queue until exhausted, yielding processed output.

        Converts a component that requires a logged-in user and got none into a redirect to
        `LOGIN_URL`, so a request arriving on a dead session lands on the login page instead of
        answering with a 500.  `Repository.build` is what decides that, raising `LoginRequired` no
        matter which layer rejected the user.

        """
        from .sse import sse_source_session
        from .utils import compact_hash

        roots = list(commands)
        queue = CommandQueue(roots)
        with tracing_span(
            "djhtmx.CommandProcessor.process",
            session=compact_hash(self.repo.session.id),
            roots=str(len(roots)),
        ):
            try:
                with sse_source_session(self.repo.session.id):
                    while queue:
                        yield from self._run_command(queue)
            except LoginRequired as e:
                logger.info("HTMX component %s requires a logged user", e.component_name)
                yield Redirect(LOGIN_URL)

    def _run_command(self, commands: CommandQueue) -> Generator[ProcessedCommand]:
        repo = self.repo
        command = commands.pop()
        logger.debug("COMMAND: %s", command)
        commands_to_append: list[Command] = []
        match command:
            case Execute(component_id, event_handler, event_data):
                commands.processing_component_id = component_id
                match repo.get_component_by_id(component_id):
                    case Destroy() as command:
                        yield command
                    case HtmxComponent() as component:
                        handler = getattr(component, event_handler)
                        handler_kwargs = filter_parameters(handler, event_data)
                        kind = self._get_handler_kind(component, event_handler, handler)
                        emitted_commands = _drain_commands_safely(kind, handler, **handler_kwargs)
                        yield from self._process_emitted_commands(
                            component,
                            emitted_commands,
                            commands,
                            during_execute=True,
                            method_name=event_handler,
                        )

            case HandleSSEEvents(component_id, envelopes):
                commands.processing_component_id = component_id
                match repo.get_component_by_id(component_id):
                    case Destroy():
                        # Stale consumer record: the component was destroyed elsewhere in this
                        # dispatch (or earlier) but its SSE consumer entry in Redis hasn't been
                        # cleaned up yet.  Silently skip; the browser-side OOB delete has already
                        # been (or will be) emitted by whoever destroyed it.
                        return
                    case HtmxComponent() as component:
                        handler = getattr(component, "_handle_sse_events", None)
                        if handler is None:
                            # Component dropped its SSE subscription between
                            # enqueue and dispatch; nothing to do.
                            return
                        kind = self._get_handler_kind(component, "_handle_sse_events", handler)
                        emitted_commands = []
                        for envelope in envelopes:
                            emitted_commands.extend(_drain_commands_safely(kind, handler, envelope))
                        yield from self._process_emitted_commands(
                            component,
                            emitted_commands,
                            commands,
                            during_execute=False,
                            method_name="_handle_sse_events",
                        )

            case SkipRender(component):
                commands.processing_component_id = component.id
                repo.session.store(component)

            case BuildAndRender(component_type, state, oob, parent_id):
                commands.processing_component_id = state.get("id", "")
                component = repo.build(component_type.__name__, state)
                child_id = component.id
                repo.session.register_child(parent_id, child_id)
                commands_to_append.append(Render(component, oob=oob))

            case Render(component, template, oob, lazy, context):
                commands.processing_component_id = component.id
                html = repo.render_html(
                    component, oob=oob, template=template, lazy=lazy, context=context
                )
                yield SendHtml(html, debug_trace=f"{component.hx_name}({component.id})")

            case Destroy(component_id) as command:
                commands.processing_component_id = component_id
                repo.unregister_component(component_id)
                yield command

            case Emit(event):
                for component in repo.get_components_by_names(*LISTENERS[type(event)]):
                    commands.processing_component_id = component.id
                    logger.debug("< AWAKED: %s id=%s", component.hx_name, component.id)
                    try:
                        handler = component._handle_event  # type: ignore[attr-defined]
                        kind = self._get_handler_kind(component, "_handle_event", handler)
                        emitted_commands = _drain_commands_unsafely(kind, handler, event)
                    except Exception as error:
                        logger.exception(
                            "HTMX unhandled error in the event handler of %s",
                            component.__class__.__name__,
                        )
                        # Don't enter a spiral of death with HtmxUnhandledError
                        if not isinstance(event, HtmxUnhandledError):
                            emitted_commands = [Emit(HtmxUnhandledError(error))]
                        else:
                            raise
                    yield from self._process_emitted_commands(
                        component,
                        emitted_commands,
                        commands,
                        during_execute=False,
                        method_name="_handle_event",
                    )

            case Signal(signals):
                commands.processing_component_id = ""
                for component_or_destroy in repo.get_components_subscribed_to(signals):
                    match component_or_destroy:
                        case Destroy() as command:
                            yield command
                        case component:
                            logger.debug("< AWAKED: %s id=%s", component.hx_name, component.id)
                            commands_to_append.append(Render(component))

            case InvalidateModelCache(model_class, pk):
                commands.processing_component_id = ""
                repo.invalidate_model_cache(model_class, pk)

            case (
                Open()
                | ReplaceURL()
                | PushURL()
                | Redirect()
                | Focus()
                | ScrollIntoView()
                | DispatchDOMEvent() as command
            ):
                commands.processing_component_id = ""
                yield command

        commands.extend(commands_to_append)
        if repo.session.is_dirty:
            repo.session.flush()

    def _process_emitted_commands(
        self,
        component: HtmxComponent,
        emitted_commands: Iterable[Command] | None,
        commands: CommandQueue,
        during_execute: bool,
        method_name: str | None = None,
    ) -> Generator[ProcessedCommand]:
        """Normalise the commands a handler emitted for `component`.

        Shared post-processing for the three handler entry points (`Execute`, `Emit` fan-out,
        `HandleSSEEvents`).  Rules:

        - If the handler returns `None` or yields nothing, enqueue an implicit default
          `Render(component)`.

        - If the handler yields `SkipRender(component)` (i.e. of the same component being handled),
          suppress the implicit default render for this invocation.  Other yielded commands still
          take effect.

        - An explicit `Render(component)` likewise stands in for the default render.

        - Under `during_execute=True` (HTTP direct event handler), the default render — and any
          partial `Render` for the same component with `lazy is None` — is forced non-lazy.  In the
          `Emit`/`HandleSSEEvents` paths the default render respects `component.lazy`.

        - Query-patcher parameter changes emit a `ReplaceURL` and a `Signal` for subscribers.

        """
        repo = self.repo
        component_was_rendered = False
        commands_to_add: list[Command | InternalCommand] = []
        for command in emitted_commands or []:
            self._record_command(command, component, method_name)
            if method_name:
                logger.debug("< YIELD: %s.%s -> %s", component.hx_name, method_name, command)
            component_was_rendered = component_was_rendered or (
                isinstance(command, SkipRender | Render) and command.component.id == component.id
            )
            if (
                component_was_rendered
                and during_execute
                and isinstance(command, Render)
                and command.lazy is None
            ):
                # make partial updates not lazy during_execute
                command.lazy = False
            if isinstance(command, InvalidateModelCache):
                # Not queued: the other listeners of the same Emit, and the other consumers of the
                # same SSE wakeup, are hydrated before the queue would reach it.
                repo.invalidate_model_cache(command.model_class, command.pk)
            else:
                commands_to_add.append(command)

        if not component_was_rendered:
            commands_to_add.append(
                Render(component, lazy=False if during_execute else component.lazy)
            )

        if signals := repo.update_params_from(component):
            yield ReplaceURL.from_params(repo.params)
            commands_to_add.append(Signal({(signal, component.id) for signal in signals}))

        commands.extend(commands_to_add)
        repo.session.store(component)

    @staticmethod
    def _get_handler_kind(component: HtmxComponent, name: str, handler: Callable) -> HandlerKind:
        """The shape of `component`'s `name` handler, as recorded at registration.

        Only public components are registered; for any other the handler is
        inspected directly, which answers the same.

        """
        if (registered := REGISTRY.get(component.hx_name)) and (
            kind := registered.handler_kind_mapping.get(name)
        ):
            return kind
        else:
            return get_handler_kind(handler)

    @classmethod
    @contextmanager
    def _install_recorder(cls) -> Iterator[CommandRecorder]:
        """Record the commands handlers yield while the block runs.

        The recorder lives in a `ContextVar`, so it also reaches the handlers that run on another
        thread inside the block: the SSE drain goes through `sse_executor.submit_sse_render`, which
        carries the context over to whichever thread renders.

        A recorder already installed is reused instead of shadowed, so several watchers open at
        once (`with htmx.assertYields(Redirect), htmx.assertEmits(Message)`) share one tape and
        each reads the window that opened with it.

        """
        if (recorder := _recorder.get()) is not None:
            yield recorder
        else:
            recorder = CommandRecorder()
            token = _recorder.set(recorder)
            try:
                yield recorder
            finally:
                _recorder.reset(token)

    def _record_command(
        self,
        command: Command,
        component: HtmxComponent,
        method_name: str | None,
    ) -> None:
        """Hand a command a handler just yielded to the recorder, if one is installed.

        Only what a handler yields reaches here.  The commands djhtmx adds on its own -- the
        default `Render` for a handler that yielded nothing, the `ReplaceURL`/`Signal` pair a
        query patcher produces, the `SendHtml` a `Render` becomes -- are added elsewhere and are
        deliberately out of the tape: a test that asks what a block yielded means the handlers'
        yields.

        """
        if (recorder := _recorder.get()) is not None:
            source = f"{component.hx_name}.{method_name}" if method_name else component.hx_name
            recorder.commands.append(RecordedCommand(command=command, source=source))


@dataclass(frozen=True, slots=True)
class RecordedCommand:
    """A command a handler yielded, and where it came from.

    `source` names the handler that yielded it (`TodoItem.toggle_editing`).

    """

    command: Command
    source: str

    def describe(self) -> str:
        """Answer with the command and the handler that yielded it, in one short piece of text.

        A command carrying a component names it by hx-name and id rather than printing its whole
        state, which is what keeps an assertion message readable.

        """
        match self.command:
            case Emit(event=event):
                described = f"Emit({event!r})"
            case Render(component=component) | SkipRender(component=component) as command:
                described = f"{type(command).__name__}({component.hx_name}#{component.id})"
            case (
                BuildAndRender()
                | Destroy()
                | Open()
                | Focus()
                | ScrollIntoView()
                | Redirect()
                | DispatchDOMEvent()
                | PushURL()
                | ReplaceURL()
                | Execute()
                | InvalidateModelCache() as command
            ):
                described = repr(command)
            case unreachable:
                assert_never(unreachable)
        return f"{self.source} -> {described}"


class CommandRecorder:
    """The tape of commands handlers yielded while it was installed.

    One recorder serves every watcher open at the same time: a watcher keeps the length of the
    tape at the moment it opened and reads through `since`, so what it sees is what the handlers
    yielded inside it.

    """

    def __init__(self):
        self.commands: list[RecordedCommand] = []

    def since(self, start: int) -> list[RecordedCommand]:
        """The commands recorded after `start`."""
        return self.commands[start:]


type SyncHandlerResult = Iterable[Command] | None
"""What a ``function`` handler returns, or the generator a ``generator`` handler hands back."""

type HandlerResult = SyncHandlerResult | Awaitable[SyncHandlerResult] | AsyncIterable[Command]
"""What calling a handler of any `HandlerKind`:obj: hands back."""


def _drain_commands_safely[**P](
    kind: HandlerKind, handler: Callable[P, HandlerResult], *args: P.args, **kwargs: P.kwargs
) -> list[Command]:
    """Drains the `handler` for commands.

    Any error while calling the handler or during the drain gets converted
    trapped and transformed to an ``Emit`` of ``HtmxUnhandledError``.  Any annotation (given by
    `annotate_handler`:func:) gets recorded.  If `trap_error` is False, errors are propagated.

    """
    try:
        return _drain_commands_unsafely(kind, handler, *args, **kwargs)
    except Exception as error:
        annotations = getattr(handler, "_htmx_annotations_", None)
        logger.exception("HTMX unhandled exception in component")
        return [Emit(HtmxUnhandledError(error, handler_annotations=annotations))]


def _drain_commands_unsafely[**P](
    kind: HandlerKind, handler: Callable[P, HandlerResult], *args: P.args, **kwargs: P.kwargs
) -> list[Command]:
    """Run `handler`, whose shape is `kind`, to completion and return its commands as a list.

    The pipeline runs synchronously on a sync-work pool thread, so this is the boundary that lets
    components mix sync and async handlers freely:

    - ``function`` / ``generator`` run directly on this pool thread, which owns the DB connection
      (see `_drain_sync_handler`:func:).
    - ``coroutine`` runs via ``async_to_sync`` on this pool thread.  ``async_to_sync`` makes the
      pool thread the thread-sensitive thread, so any Django async ORM the handler awaits runs on
      this same thread and shares its single connection -- and any transaction open on it.
    - ``async_generator`` is consumed with ``async for`` under the same ``async_to_sync`` bridge,
      and only outside a transaction.

    A handler returning ``None`` normalises to ``[]``, preserving the "no explicit render means
    default render" semantics of `CommandProcessor._process_emitted_commands`:meth:.

    """
    match kind:
        case "async_generator":
            return async_to_sync(_drain_async_handler)(
                cast(Callable[P, AsyncIterable[Command]], handler), *args, **kwargs
            )
        case "coroutine":
            return async_to_sync(_await_handler)(
                cast(Callable[P, Awaitable[SyncHandlerResult]], handler), *args, **kwargs
            )
        case "function" | "generator":
            return _drain_sync_handler(
                cast(Callable[P, SyncHandlerResult], handler), *args, **kwargs
            )
        case unreachable:
            assert_never(unreachable)


async def _await_handler[**P](
    handler: Callable[P, Awaitable[SyncHandlerResult]], /, *args: P.args, **kwargs: P.kwargs
) -> list[Command]:
    result = await handler(*args, **kwargs)
    return [] if result is None else list(result)


async def _drain_async_handler[**P](
    handler: Callable[P, AsyncIterable[Command]], /, *args: P.args, **kwargs: P.kwargs
) -> list[Command]:
    return [command async for command in handler(*args, **kwargs)]


def _drain_sync_handler[**P](
    handler: Callable[P, SyncHandlerResult], /, *args: P.args, **kwargs: P.kwargs
) -> list[Command]:
    """Run a synchronous handler to completion on the sync-work pool thread.

    Handles both plain functions (returning a list/None) and generator functions (yielding
    commands): the generator is fully drained here, on the worker thread that owns the DB
    connection, so no lazy iteration leaks back onto the event loop.

    Honours `ATOMIC_REQUESTS`, but only where the dispatch did not already open a transaction.
    `Repository.atomic_dispatch`:meth: normally wraps the whole dispatch, and this handler then
    simply runs inside it, so the cascade commits or rolls back as one unit.  Where it did not -- a
    streaming dispatch, which cannot hold a transaction for the length of its stream -- the handler
    gets one of its own and a failure rolls back its writes alone.

    """
    with atomic_if_requested():
        result = handler(*args, **kwargs)
        return [] if result is None else list(result)


_recorder: ContextVar[CommandRecorder | None] = ContextVar("djhtmx.command_recorder", default=None)


__all__ = ["CommandProcessor", "CommandRecorder", "RecordedCommand"]
