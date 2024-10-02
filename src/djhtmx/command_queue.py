from typing import Iterable

from .component import (
    BuildAndRender,
    Command,
    Destroy,
    DispatchEvent,
    Emit,
    Execute,
    Focus,
    Redirect,
    Render,
    Signal,
    SkipRender,
)


class CommandQueue:
    def __init__(self, commands: list[Command]):
        self._commands = commands
        self._destroyed_ids: set[str] = set()
        self._optimize()

    def extend(self, commands: Iterable[Command]):
        self._commands.extend(commands)
        self._optimize()

    def append(self, command: Command):
        self._commands.append(command)
        self._optimize()

    def pop(self) -> Command:
        return self._commands.pop(0)

    def __bool__(self):
        return bool(self._commands)

    def _optimize(self):
        self._commands.sort(key=self._priority)
        new_commands = []
        for i, command in enumerate(self._commands):
            match command:
                case (
                    Execute()
                    | Signal()
                    | Emit()
                    | SkipRender()
                    | Focus()
                    | Redirect()
                    | DispatchEvent() as command
                ):
                    new_commands.append(command)

                case Destroy(component_id) as command:
                    self._destroyed_ids.add(component_id)
                    new_commands.append(command)

                case BuildAndRender(_, state, _) as command:
                    if not (
                        (component_id := state.get("id")) and component_id in self._destroyed_ids
                    ):
                        new_commands.append(command)

                case Render(component) as command:
                    if component.id not in self._destroyed_ids and not any(
                        isinstance(ahead_command, Render)
                        and ahead_command.component.id == component.id
                        and ahead_command.template is None
                        for ahead_command in self._commands[i + 1 :]
                    ):
                        new_commands.append(command)

        self._commands = new_commands

    @staticmethod
    def _priority(command: Command) -> tuple[int, str, int]:
        match command:
            case Execute():
                return 0, "None", 0
            case Signal():
                return 1, "None", 0
            case Emit():
                return 2, "None", 0
            case Destroy():
                return 3, "None", 0
            case SkipRender():
                return 4, "None", 0
            case BuildAndRender():
                return 5, "None", 0
            case Render(component, template, _, timestamp):
                if template:
                    return 6, component.id, timestamp
                else:
                    return 7, component.id, timestamp
            case Focus() | Redirect() | DispatchEvent():
                return 8, "", 0