# Copyright 2026 Google Inc.
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
from urllib.parse import parse_qs, urlsplit

from websockets.asyncio.client import connect

from colab_mcp.session import CONNECTION_TOOL_NAME, ColabSessionProxy
from colab_mcp.websocket_server import COLAB
from minimcp import RpcError, ServerSession
from conftest import Wire, initialize_browser, receive_browser, reply_browser


def browser_connection(proxy):
    return connect(
        f"ws://127.0.0.1:{proxy.wss.port}?access_token={proxy.wss.token}",
        origin=COLAB, subprotocols=["mcp"], proxy=None,
    )


def test_protocol_lifecycle_and_errors():
    async def scenario():
        async def unsupported(method, params):
            raise RpcError(-32601, "Method not found")

        wire = Wire()
        async with ServerSession(
            wire, name="test", version="1", capabilities={}, on_request=unsupported,
        ):
            await wire.feed("this is not JSON")
            assert (await wire.response(None))["error"]["code"] == -32700
            assert (await wire.call(1, "tools/list"))["error"]["code"] == -32000
            result = await wire.call(2, "initialize", {
                "protocolVersion": "unknown-version",
                "capabilities": {}, "clientInfo": {"name": "test", "version": "1"},
            })
            assert result["result"]["protocolVersion"] == "2025-11-25"
            assert (await wire.call(3, "tools/list"))["error"]["code"] == -32000
            await wire.feed({"jsonrpc": "2.0", "method": "notifications/initialized"})
            assert (await wire.call(4, "unknown"))["error"]["code"] == -32601
            assert (await wire.call(5, "ping"))["result"] == {}
            assert (await wire.call(6, "initialize"))["error"]["code"] == -32600

    asyncio.run(scenario())


def test_discovery_and_bidirectional_requests():
    async def scenario():
        wire = Wire()
        async with ColabSessionProxy(wire) as proxy:
            await wire.initialize({"roots": {"listChanged": True}})
            before = await wire.call(1, "tools/list")
            assert [tool["name"] for tool in before["result"]["tools"]] == [CONNECTION_TOOL_NAME]
            async with browser_connection(proxy) as browser:
                init = await initialize_browser(browser, proxy, {
                    "tools": {}, "resources": {}, "prompts": {},
                })
                assert init["params"]["capabilities"] == {"roots": {"listChanged": True}}
                await wire.take(lambda msg: msg.get("method") == "notifications/tools/list_changed")
                remote_tool = {
                    "name": "read_notebook", "inputSchema": {"type": "object"},
                    "annotations": {"readOnlyHint": True},
                }
                await wire.feed({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
                request = await receive_browser(browser)
                await reply_browser(browser, request, {"tools": [remote_tool], "nextCursor": "page2"})
                listing = (await wire.response(2))["result"]
                assert listing["tools"][1] == remote_tool
                assert listing["nextCursor"] == "page2"
                await wire.feed({
                    "jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {"cursor": "page2"},
                })
                request = await receive_browser(browser)
                assert request["params"] == {"cursor": "page2"}
                await reply_browser(browser, request, {"tools": [remote_tool]})
                assert (await wire.response(3))["result"]["tools"] == [remote_tool]

                params = {
                    "name": "read_notebook", "arguments": {},
                    "_meta": {"progressToken": "progress-1"},
                }
                await wire.feed({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": params})
                call = await receive_browser(browser)
                assert call["params"] == params
                # The same ID may be in flight simultaneously in both directions.
                await browser.send(json.dumps({
                    "jsonrpc": "2.0", "id": call["id"], "method": "roots/list",
                }))
                reverse = await wire.take(lambda msg: msg.get("method") == "roots/list")
                roots = {"roots": [{"uri": "file:///workspace", "name": "Workspace"}]}
                await wire.feed({"jsonrpc": "2.0", "id": reverse["id"], "result": roots})
                assert (await receive_browser(browser)) == {
                    "jsonrpc": "2.0", "id": call["id"], "result": roots,
                }
                progress = {"progressToken": "progress-1", "progress": 1, "total": 2}
                await browser.send(json.dumps({
                    "jsonrpc": "2.0", "method": "notifications/progress", "params": progress,
                }))
                assert (await wire.take(lambda msg: msg.get("method") == "notifications/progress"))["params"] == progress
                result = {
                    "content": [
                        {"type": "text", "text": "فارسی 🐍"},
                        {"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"},
                    ],
                    "structuredContent": {"cells": [1, 2]}, "isError": False,
                    "_meta": {"extension": "preserved"},
                }
                await reply_browser(browser, call, result)
                assert (await wire.response(4))["result"] == result
                for request_id, method, result in [
                    (5, "resources/list", {"resources": []}),
                    (6, "prompts/list", {"prompts": []}),
                ]:
                    await wire.feed({"jsonrpc": "2.0", "id": request_id, "method": method})
                    request = await receive_browser(browser)
                    assert request["method"] == method
                    await reply_browser(browser, request, result)
                    assert (await wire.response(request_id))["result"] == result

    asyncio.run(scenario())


def test_cancellation_errors_and_reconnection():
    async def scenario():
        wire = Wire()
        async with ColabSessionProxy(wire) as proxy:
            await wire.initialize()
            async with browser_connection(proxy) as browser:
                await initialize_browser(browser, proxy)
                await wire.take(lambda msg: msg.get("method") == "notifications/tools/list_changed")
                await wire.feed({
                    "jsonrpc": "2.0", "id": 10, "method": "tools/call", "params": {"name": "slow"},
                })
                request = await receive_browser(browser)
                # A long call must not block other requests on the same connection.
                assert (await wire.call(11, "ping"))["result"] == {}
                await wire.feed({
                    "jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 10},
                })
                cancelled = await receive_browser(browser)
                assert cancelled["method"] == "notifications/cancelled"
                assert cancelled["params"]["requestId"] == request["id"]
                await reply_browser(browser, request, {"content": []})
                await wire.feed({
                    "jsonrpc": "2.0", "id": 12, "method": "tools/call", "params": {"name": "bad"},
                })
                request = await receive_browser(browser)
                remote_error = {
                    "code": -32042, "message": "User action needed", "data": {"detail": "keep this"},
                }
                await reply_browser(browser, request, error=remote_error)
                assert (await wire.response(12))["error"] == remote_error
                await wire.feed({
                    "jsonrpc": "2.0", "id": 13, "method": "tools/call", "params": {"name": "interrupted"},
                })
                await receive_browser(browser)
            assert (await wire.response(13))["error"]["code"] == -32000
            await wire.take(lambda msg: msg.get("method") == "notifications/tools/list_changed")
            assert not proxy.connected.is_set()
            assert not any(message.get("id") == 10 for message in wire.saved)
            async with browser_connection(proxy) as replacement:
                await initialize_browser(replacement, proxy)
                await wire.feed({"jsonrpc": "2.0", "id": 14, "method": "tools/list"})
                request = await receive_browser(replacement)
                new_tool = {"name": "new_notebook_tool", "inputSchema": {"type": "object"}}
                await reply_browser(replacement, request, {"tools": [new_tool]})
                assert (await wire.response(14))["result"]["tools"][1:] == [new_tool]

    asyncio.run(scenario())


def test_connection_tool_fallback_and_url_boundary(monkeypatch):
    opened = []

    def open_browser(url):
        opened.append(url)
        return False

    monkeypatch.setattr("webbrowser.open_new", open_browser)

    async def scenario():
        wire = Wire()
        async with ColabSessionProxy(wire) as proxy:
            await wire.initialize()
            response = await wire.call(1, "tools/call", {
                "name": CONNECTION_TOOL_NAME,
                "arguments": {"notebook_url": COLAB + "/drive/notebook-id?authuser=1#scrollTo=cell"},
            })
            assert response["result"]["structuredContent"] == {"result": False}
            url = urlsplit(opened[0])
            assert url.path == "/drive/notebook-id" and url.query == "authuser=1"
            assert parse_qs(url.fragment) == {
                "scrollTo": ["cell"], "mcpProxyToken": [proxy.wss.token],
                "mcpProxyPort": [str(proxy.wss.port)],
            }
            for invalid in [
                "https://evil.example/notebook", "https://colab.research.google.com@evil.example/",
            ]:
                response = await wire.call(2, "tools/call", {
                    "name": CONNECTION_TOOL_NAME, "arguments": {"notebook_url": invalid},
                })
                assert response["error"]["code"] == -32602
            assert len(opened) == 1

    asyncio.run(scenario())


def test_connection_tool_waits_for_browser(monkeypatch):
    async def scenario():
        opened = asyncio.Event()
        loop = asyncio.get_running_loop()

        def open_browser(url):
            loop.call_soon_threadsafe(opened.set)
            return True

        monkeypatch.setattr("webbrowser.open_new", open_browser)
        wire = Wire()
        async with ColabSessionProxy(wire) as proxy:
            await wire.initialize()
            await wire.feed({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": CONNECTION_TOOL_NAME, "_meta": {"progressToken": 0}},
            })
            await asyncio.wait_for(opened.wait(), 5)
            assert (await wire.call(2, "ping"))["result"] == {}
            async with browser_connection(proxy) as browser:
                await initialize_browser(browser, proxy)
                result = await wire.response(1)
                assert result["result"]["structuredContent"] == {"result": True}
                progress = await wire.take(
                    lambda msg: msg.get("method") == "notifications/progress"
                    and msg["params"]["progress"] == 2
                )
                assert progress["params"]["progressToken"] == 0

    asyncio.run(scenario())
