from collections.abc import Iterable
from typing import NamedTuple, assert_never

from django.db import models
from django.db.models.signals import post_save, pre_delete

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
    PushURL,
    Redirect,
    Render,
    ReplaceURL,
    ScrollIntoView,
    Signal,
    SkipRender,
)
from .introspection import get_related_fields
from .utils import get_model_subscriptions

QueuedCommand = Command | InternalCommand


class CommandPriority(NamedTuple):
    """Sort key for `CommandQueue._optimize`.

    `bucket` groups commands by processing order (lower = earlier).
    `key` disambiguates within a bucket (typically a `component_id`).
    `timestamp` is the command's monotonic-ns creation time, used as
    a final tiebreaker so commands settle in FIFO order within a key.
    """

    bucket: int
    key: str
    timestamp: int


class CommandQueue:
    def __init__(self, commands: list[QueuedCommand]):
        self.processing_component_id: str = ""
        self._commands = commands
        self._destroyed_ids: set[str] = set()
        self._optimize()

        # subscribe to signals changes
        post_save.connect(self._listen_to_post_save_and_pre_delete, weak=True)
        pre_delete.connect(self._listen_to_post_save_and_pre_delete, weak=True)

    def _listen_to_post_save_and_pre_delete(
        self,
        sender: type[models.Model],
        instance: models.Model,
        created: bool | None = None,
        **kwargs,
    ):
        if created is None:
            action = "deleted"
        elif created:
            action = "created"
        else:
            action = "updated"

        signals = get_model_subscriptions(
            instance, actions=(action, None)
        ) | get_model_subscriptions(type(instance), actions=(action, None))

        for field in get_related_fields(sender):
            fk_id = getattr(instance, field.name)
            signal = f"{field.related_model_name}.{fk_id}.{field.relation_name}"
            signals.update((signal, f"{signal}.{action}"))

        if signals:
            self.extend([Signal({(signal, self.processing_component_id) for signal in signals})])

    def extend(self, commands: Iterable[QueuedCommand]):
        commands = list(commands)
        if commands:
            self._commands.extend(commands)
            self._optimize()

    def append(self, command: QueuedCommand):
        self._commands.append(command)
        self._optimize()

    def pop(self) -> QueuedCommand:
        return self._commands.pop(0)

    def __bool__(self):
        return bool(self._commands)

    def _optimize(self):
        self._commands.sort(key=self._priority)
        new_commands: list[QueuedCommand] = []
        for i, command in enumerate(self._commands):
            match command:
                case (
                    Execute()
                    | Signal()
                    | HandleSSEEvents()
                    | Emit()
                    | SkipRender()
                    | Focus()
                    | ScrollIntoView()
                    | Redirect()
                    | DispatchDOMEvent()
                    | Open()
                    | PushURL()
                    | ReplaceURL()
                ):
                    new_commands.append(command)

                case Destroy(component_id) as command:
                    # Register destroyed ids
                    self._destroyed_ids.add(component_id)
                    new_commands.append(command)

                case BuildAndRender(_, state, _) as command:
                    # Remove BuildAndRender of destroyed ids
                    if not (
                        (component_id := state.get("id")) and component_id in self._destroyed_ids
                    ):
                        new_commands.append(command)

                case Render(component) as command:
                    # Remove Render of destroyed ids
                    # Let the latest Render of the same component survive, kill the rest
                    if component.id not in self._destroyed_ids and not any(
                        isinstance(ahead_command, Render)
                        and ahead_command.component.id == component.id
                        and ahead_command.template is None
                        for ahead_command in self._commands[i + 1 :]
                    ):
                        new_commands.append(command)

        self._commands = new_commands

    @staticmethod
    def _priority(command: QueuedCommand) -> CommandPriority:
        match command:
            case Execute() | HandleSSEEvents():
                return CommandPriority(bucket=0, key="", timestamp=0)
            case Destroy(component_id):
                return CommandPriority(bucket=10, key=component_id, timestamp=0)
            case Signal(_, timestamp):
                return CommandPriority(bucket=20, key="", timestamp=timestamp)
            case Emit(_, timestamp):
                return CommandPriority(bucket=30, key="", timestamp=timestamp)
            case SkipRender():
                return CommandPriority(bucket=40, key="", timestamp=0)
            case BuildAndRender(_, _, _, _, timestamp):
                return CommandPriority(bucket=50, key="", timestamp=timestamp)
            case Render(component, template, _, _, _, timestamp):
                bucket = 60 if template else 70
                return CommandPriority(bucket=bucket, key=component.id, timestamp=timestamp)
            case (
                Focus()
                | ScrollIntoView()
                | Redirect()
                | DispatchDOMEvent()
                | Open()
                | ReplaceURL()
                | PushURL()
            ):
                return CommandPriority(bucket=80, key="", timestamp=0)
            case unreachable:
                assert_never(unreachable)
