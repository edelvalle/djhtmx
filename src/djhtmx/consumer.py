import logging
from typing import Any, Literal, assert_never

from channels.generic.websocket import AsyncJsonWebsocketConsumer
from pydantic import BaseModel, TypeAdapter

from . import json
from .commands import PushURL, ReplaceURL, SendHtml
from .component import Command, Destroy, DispatchDOMEvent, Focus, Open, Redirect
from .introspection import parse_request_data
from .repo import Repository
from .utils import get_params


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
        self.repo = Repository.from_websocket(self.scope["user"])

    async def disconnect(self, code):
        await super().disconnect(code)

    async def receive_json(self, event_data: dict[str, Any]):
        if headers := event_data.pop("HEADERS", None):
            url = headers["HX-Current-URL"]
            component_id = headers["HX-Component-Id"]
            event_handler = headers["HX-Component-Handler"]
            params = get_params(url)
            logger.debug(">>>> Call: %s %s", component_id, event_handler)
            self.repo.params.clear()
            self.repo.params.update(params)  # type: ignore
            # Command dispatching
            async for command in self.repo.adispatch_event(
                component_id, event_handler, parse_request_data(event_data)
            ):
                match command:
                    case SendHtml(html, debug_trace):
                        logger.debug(
                            "< Command: %s", f"SendHtml[{debug_trace}](... {len(html)} ...)"
                        )
                        await self.send(html)
                    case (
                        Destroy()
                        | Redirect()
                        | Focus()
                        | DispatchDOMEvent()
                        | PushURL()
                        | Open()
                        | ReplaceURL()
                    ):
                        logger.debug("< Command: %s", command)
                        await self.send_json(command)
                    case _ as unreachable:
                        assert_never(unreachable)

    async def send_commands(self, commands: list[Command]):
        for command in commands:
            await self.send_json(command)

    @classmethod
    async def decode_json(cls, text_data) -> dict[str, Any]:
        return json.loads(text_data)

    @classmethod
    async def encode_json(cls, content) -> str:
        return json.dumps(content)


logger = logging.getLogger(__name__)
