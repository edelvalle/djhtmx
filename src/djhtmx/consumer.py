import json

from channels.generic.websocket import AsyncJsonWebsocketConsumer


class Consumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        print(self.scope)
        await self.accept()

    def disconnect(self, close_code):
        pass

    async def receive(self, text_data):
        text_data_json = json.loads(text_data)
        message = text_data_json["message"]

        await self.send(text_data=json.dumps({"message": message}))
