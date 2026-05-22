"""Command processor for djhtmx.

Owns the command loop that drives an event from its initial `Execute` (or
other root command) through to the stream of `ProcessedCommand` values that
the transport layer (HTTP endpoint, SSE renderer) turns into wire output.

This module is the single source of truth for command semantics.
`Repository` keeps responsibility for component lifecycle, session storage,
template rendering, and query parameter patching — i.e. the *state* a
command consults — but the *decisions* about what each command means live
here.

See `docs/plans/sse-generalized-worker.md` for the rationale behind the
HTTP/SSE unification this enables.
"""

from __future__ import annotations

import logging
from collections.abc import Generator, Iterable
from typing import TYPE_CHECKING

from pydantic import ValidationError

from djhtmx.global_events import HtmxUnhandledError

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
from .introspection import filter_parameters
from .settings import LOGIN_URL

if TYPE_CHECKING:
    from .repo import Repository

logger = logging.getLogger(__name__)


class CommandProcessor:
    """Run djhtmx commands against a `Repository` and yield processed commands.

    Instantiated per command run.  The processor is stateless beyond its
    repository reference; all session and component state lives on the
    `Repository`.
    """

    def __init__(self, repo: Repository):
        self.repo = repo

    def process(self, commands: Iterable[Command | InternalCommand]) -> Generator[ProcessedCommand]:
        """Drive the command queue until exhausted, yielding processed output.

        Catches `ValidationError`s whose root cause is an invalid `user`
        and converts them into a redirect to `LOGIN_URL`, matching the
        previous `Repository.dispatch_event` behavior.
        """
        from .sse import sse_source_session

        queue = CommandQueue(list(commands))
        try:
            with sse_source_session(self.repo.session.id):
                while queue:
                    yield from self._run_command(queue)
        except ValidationError as e:
            if any(
                e
                for error in e.errors()
                if error["type"] == "is_instance_of" and error["loc"] == ("user",)
            ):
                yield Redirect(LOGIN_URL)
            else:
                raise

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
                            emited_commands = handler(**handler_kwargs)
                        except Exception as error:
                            annotations = getattr(handler, "_htmx_annotations_", None)
                            logger.exception(
                                "HTMX unhandled exception in component %s",
                                component.__class__.__name__,
                            )
                            emited_commands = [
                                Emit(HtmxUnhandledError(error, handler_annotations=annotations))
                            ]
                        yield from self._process_emited_commands(
                            component,
                            emited_commands,
                            commands,
                            during_execute=True,
                            method_name=event_handler,
                        )

            case HandleSSEEvents(component_id, envelopes):
                commands.processing_component_id = component_id
                match repo.get_component_by_id(component_id):
                    case Destroy() as command:
                        yield command
                    case HtmxComponent() as component:
                        handler = getattr(component, "_handle_sse_events", None)
                        if handler is None:
                            # Component dropped its SSE subscription between
                            # enqueue and dispatch; nothing to do.
                            return
                        emited_commands: list[Command] = []
                        for envelope in envelopes:
                            try:
                                yielded = handler(envelope)
                            except Exception as error:
                                logger.exception(
                                    "HTMX unhandled exception in _handle_sse_events of %s",
                                    component.__class__.__name__,
                                )
                                if not isinstance(error, HtmxUnhandledError):
                                    emited_commands.append(Emit(HtmxUnhandledError(error)))
                                continue
                            if yielded is not None:
                                emited_commands.extend(c for c in yielded if c is not None)
                        yield from self._process_emited_commands(
                            component,
                            emited_commands,
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
                        emited_commands = component._handle_event(event)  # type: ignore
                    except Exception as error:
                        logger.exception(
                            "HTMX unhandled error in the event handler of %s",
                            component.__class__.__name__,
                        )
                        # Don't enter a spiral of death with HtmxUnhandledError
                        if not isinstance(event, HtmxUnhandledError):
                            emited_commands = [Emit(HtmxUnhandledError(error))]
                        else:
                            raise
                    yield from self._process_emited_commands(
                        component,
                        emited_commands,
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
        repo.session.flush()

    def _process_emited_commands(
        self,
        component: HtmxComponent,
        emmited_commands: Iterable[Command] | None,
        commands: CommandQueue,
        during_execute: bool,
        method_name: str | None = None,
    ) -> Iterable[ProcessedCommand]:
        repo = self.repo
        component_was_rendered = False
        commands_to_add: list[Command | InternalCommand] = []
        for command in emmited_commands or []:
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


__all__ = ["CommandProcessor"]
