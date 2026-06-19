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

import contextlib
import logging
from collections.abc import Callable, Generator, Iterable
from inspect import isasyncgenfunction
from typing import TYPE_CHECKING, Any

from asgiref.sync import async_to_sync, iscoroutinefunction
from django.db import connections, transaction

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
    HtmxComponent,
)
from .exceptions import LoginRequired
from .introspection import filter_parameters
from .settings import LOGIN_URL

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

        queue = CommandQueue(list(commands))
        with tracing_span(
            "djhtmx.CommandProcessor.process",
            session=compact_hash(self.repo.session.id),
            roots=str(len(queue._commands)),
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
                        try:
                            emitted_commands = self._invoke_handler(handler, **handler_kwargs)
                        except Exception as error:
                            annotations = getattr(handler, "_htmx_annotations_", None)
                            logger.exception(
                                "HTMX unhandled exception in component %s",
                                component.__class__.__name__,
                            )
                            emitted_commands = [
                                Emit(HtmxUnhandledError(error, handler_annotations=annotations))
                            ]
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
                        # Stale consumer record: the component was destroyed
                        # elsewhere in this dispatch (or earlier) but its SSE
                        # consumer entry in Redis hasn't been cleaned up yet.
                        # Silently skip; the browser-side OOB delete has
                        # already been (or will be) emitted by whoever
                        # destroyed it.
                        return
                    case HtmxComponent() as component:
                        handler = getattr(component, "_handle_sse_events", None)
                        if handler is None:
                            # Component dropped its SSE subscription between
                            # enqueue and dispatch; nothing to do.
                            return
                        emitted_commands = []
                        for envelope in envelopes:
                            try:
                                emitted_commands.extend(self._invoke_handler(handler, envelope))
                            except Exception as error:
                                logger.exception(
                                    "HTMX unhandled exception in _handle_sse_events of %s",
                                    component.__class__.__name__,
                                )
                                if not isinstance(error, HtmxUnhandledError):
                                    emitted_commands.append(Emit(HtmxUnhandledError(error)))
                                continue
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
                        emitted_commands = self._invoke_handler(component._handle_event, event)  # type: ignore
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
    def _invoke_handler(handler: Callable, /, *args: Any, **kwargs: Any) -> list:
        """Invoke an event handler of any shape and return its commands as a list.

        The pipeline runs synchronously on a sync-work pool thread, so this is the
        auto-wrap boundary that lets components mix sync and async handlers freely:

        - plain ``def`` / sync generator   -> run directly on this pool thread, which
          owns the DB connection; ``ATOMIC_REQUESTS`` is applied per handler (see
          :meth:`_drain_sync_handler`).
        - ``async def`` (coroutine)        -> run via ``async_to_sync`` on this pool
          thread.  ``async_to_sync`` makes the pool thread the thread-sensitive
          thread, so any Django async ORM the handler awaits runs on this same
          thread and shares its single connection.
        - ``async def`` + ``yield``        -> consumed with ``async for`` under the
          same ``async_to_sync`` bridge.

        A handler returning ``None`` normalises to ``[]``, preserving the
        "no explicit render ⇒ default render" semantics of
        :meth:`_process_emitted_commands`.
        """
        # Detection unwraps bound methods; `iscoroutinefunction` is asgiref's, which
        # also honours `markcoroutinefunction`-marked callables and pydantic's
        # `validate_call` async wrappers.
        if isasyncgenfunction(handler):
            return async_to_sync(CommandProcessor._drain_async_handler)(handler, args, kwargs)
        elif iscoroutinefunction(handler):
            return async_to_sync(CommandProcessor._await_handler)(handler, args, kwargs)
        else:
            return CommandProcessor._drain_sync_handler(handler, args, kwargs)

    @staticmethod
    async def _await_handler(handler: Callable, args: tuple, kwargs: dict) -> list:
        result = await handler(*args, **kwargs)
        return [] if result is None else list(result)

    @staticmethod
    async def _drain_async_handler(handler: Callable, args: tuple, kwargs: dict) -> list:
        return [command async for command in handler(*args, **kwargs)]

    @staticmethod
    def _drain_sync_handler(handler: Callable, args: tuple, kwargs: dict) -> list:
        """Run a synchronous handler to completion on the sync-work pool thread.

        Handles both plain functions (returning a list/None) and generator
        functions (yielding commands): the generator is fully drained here, on the
        worker thread that owns the DB connection, so no lazy iteration leaks back
        onto the event loop.

        Honours `ATOMIC_REQUESTS`: an async view bypasses Django's per-request
        atomic wrapping, so each sync handler is instead wrapped in
        `transaction.atomic` for every database configured with `ATOMIC_REQUESTS`.
        Atomicity is per-handler; a handler that raises rolls back its own writes
        before the error is surfaced.
        """
        with contextlib.ExitStack() as stack:
            for alias in connections:
                if connections[alias].settings_dict["ATOMIC_REQUESTS"]:
                    stack.enter_context(transaction.atomic(using=alias))
            result = handler(*args, **kwargs)
            return [] if result is None else list(result)


__all__ = ["CommandProcessor"]
