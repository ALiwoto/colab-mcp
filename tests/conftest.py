import asyncio
import json

import pytest


@pytest.fixture(autouse=True)
def prevent_browser_launch(monkeypatch):
    monkeypatch.setattr("webbrowser.open_new", lambda url: False)


class Wire:
    """Raw agent-side JSON messages, independent of the MCP implementation."""

    def __init__(self):
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.saved = []

    async def __aiter__(self):
        while (message := await self.incoming.get()) is not None:
            yield message

    async def send(self, message):
        await self.outgoing.put(json.loads(message))

    async def feed(self, message):
        await self.incoming.put(message if isinstance(message, str) else json.dumps(message))

    async def take(self, predicate):
        for index, message in enumerate(self.saved):
            if predicate(message):
                return self.saved.pop(index)
        async with asyncio.timeout(5):
            while True:
                message = await self.outgoing.get()
                if predicate(message):
                    return message
                self.saved.append(message)

    async def response(self, request_id):
        return await self.take(
            lambda message: message.get("id") == request_id and "method" not in message
        )

    async def call(self, request_id, method, params=None):
        await self.feed({
            "jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {},
        })
        return await self.response(request_id)

    async def initialize(self, capabilities=None):
        response = await self.call("init", "initialize", {
            "protocolVersion": "2025-11-25",
            "clientInfo": {"name": "test-agent", "version": "1"},
            "capabilities": capabilities or {},
        })
        assert "result" in response
        await self.feed({"jsonrpc": "2.0", "method": "notifications/initialized"})
        await self.call("ready", "ping")


async def receive_browser(websocket):
    return json.loads(await asyncio.wait_for(websocket.recv(), 5))


async def reply_browser(websocket, request, result=None, error=None):
    response = {"jsonrpc": "2.0", "id": request["id"]}
    response.update({"error": error} if error is not None else {"result": result})
    await websocket.send(json.dumps(response))


async def initialize_browser(websocket, proxy, capabilities=None):
    request = await receive_browser(websocket)
    assert request["method"] == "initialize"
    await reply_browser(websocket, request, {
        "protocolVersion": "2025-11-25",
        "serverInfo": {"name": "test-colab", "version": "1"},
        "capabilities": capabilities or {"tools": {}},
    })
    assert (await receive_browser(websocket))["method"] == "notifications/initialized"
    await asyncio.wait_for(proxy.connected.wait(), 5)
    return request
