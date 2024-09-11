from typing import Any, Literal, cast

from channels.db import database_sync_to_async as db  # type: ignore
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.http.request import QueryDict
from pydantic import BaseModel, TypeAdapter

from . import json
from .component import (
    Command,
    Destroy,
    DispatchEvent,
    Focus,
    PushURL,
    Redirect,
    Repository,
    SendHtml,
    SendState,
    get_params,
)


class ComponentsRemoved(BaseModel):
    type: Literal["removed"]
    component_ids: list[str]


class ComponentsAdded(BaseModel):
    type: Literal["added"]
    states: list[str]
    subscriptions: dict[str, str]


Event = ComponentsRemoved | ComponentsAdded
EventAdapter = TypeAdapter(Event)


class Consumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        await self.accept()
        self.repo = Repository(self.scope["user"], params=QueryDict(None, mutable=True))

    async def disconnect(self, code):
        await super().disconnect(code)

    async def receive_json(self, event_data: dict[str, Any]):
        if headers := event_data.pop("HEADERS", None):
            url = headers["HX-Current-URL"]
            component_id = headers["HX-Component-Id"]
            event_handler = headers["HX-Component-Handler"]
            params = get_params(url)
            print("> Call:", component_id, event_handler)
            self.repo.params.clear()
            self.repo.params.update(params)  # type: ignore

            # Command dispatching
            async for command in self.repo.dispatch_event(component_id, event_handler, event_data):
                match command:
                    case SendHtml(html):
                        await self.send(html)
                    case (
                        Destroy(_)
                        | Redirect(_)
                        | Focus(_)
                        | DispatchEvent(_)
                        | SendState(_)
                        | PushURL(_)
                    ):
                        print("< Command:", command)
                        await self.send_json(command)

        else:
            event: Event = cast(Event, EventAdapter.validate_python(event_data))
            print("> Event:", event)
            match event:
                case ComponentsRemoved(component_ids=component_ids):
                    for component_id in component_ids:
                        self.repo.unregister_component(component_id)
                case ComponentsAdded(states=states, subscriptions=subscriptions):
                    await db(self.repo.add)(states, subscriptions)

    async def send_commands(self, commands: list[Command]):
        for command in commands:
            await self.send_json(command)

    @classmethod
    async def decode_json(cls, text_data) -> dict[str, Any]:
        return json.loads(text_data)

    @classmethod
    async def encode_json(cls, content) -> str:
        return json.dumps(content)
