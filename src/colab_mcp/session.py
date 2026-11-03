# Copyright 2026 Google Inc.
# SPDX-License-Identifier: Apache-2.0

import asyncio
from contextlib import AsyncExitStack, suppress
import json
import logging
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import webbrowser

from minimcp import Peer, RpcError, ServerSession, initialize_client

from .runtime import ACTION_TOOL, RUNTIME_TOOLS, STATUS_TOOL, RuntimeBridge
from .websocket_server import COLAB, COLAB_ALT_DOMAIN, SCRATCH_PATH, ColabWebSocketServer

UI_CONNECTION_TIMEOUT = 60
CONNECTION_TOOL_NAME = "open_colab_browser_connection"
CONNECTION_TOOL = {
    "name": CONNECTION_TOOL_NAME,
    "description": (
        "Connect to a Colab notebook in the browser and unlock its notebook tools. "
        "When disconnected, opens a scratch notebook or the supplied notebook_url. "
        "Waits up to 60 seconds for the user to connect."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "notebook_url": {
                "type": "string",
                "description": "Existing HTTPS Colab notebook URL.",
            },
        },
        "additionalProperties": False,
    },
    "outputSchema": {
        "type": "object",
        "properties": {"result": {"type": "boolean"}},
        "required": ["result"],
    },
}
NOTEBOOK_LIST_TOOL = {
    "name": "list_notebook_tools",
    "description": (
        "List the connected Colab browser's tools and their input schemas. "
        "Call after open_colab_browser_connection, then use call_notebook_tool. "
        "If nextCursor is returned, pass it as cursor to read the next page."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {"cursor": {"type": "string"}},
        "additionalProperties": False,
    },
    "annotations": {"readOnlyHint": True},
}
NOTEBOOK_CALL_TOOL = {
    "name": "call_notebook_tool",
    "description": (
        "Call a tool provided by the connected Colab browser. First use "
        "list_notebook_tools to get its exact name and argument schema."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Browser tool name."},
            "arguments": {"type": "object", "description": "Arguments matching its input schema."},
        },
        "required": ["name", "arguments"],
        "additionalProperties": False,
    },
}
LOCAL_TOOLS = {
    tool["name"]: tool
    for tool in (CONNECTION_TOOL, NOTEBOOK_LIST_TOOL, NOTEBOOK_CALL_TOOL, *RUNTIME_TOOLS)
}

LIST_METHODS = {
    "tools/list": "tools",
    "resources/list": "resources",
    "resources/templates/list": "resourceTemplates",
    "prompts/list": "prompts",
}
METHOD_CAPABILITIES = {
    "tools/list": "tools",
    "tools/call": "tools",
    "resources/list": "resources",
    "resources/templates/list": "resources",
    "resources/read": "resources",
    "resources/subscribe": "resources",
    "resources/unsubscribe": "resources",
    "prompts/list": "prompts",
    "prompts/get": "prompts",
    "logging/setLevel": "logging",
    "completion/complete": "completions",
}
CLIENT_METHOD_CAPABILITIES = {
    "roots/list": "roots",
    "sampling/createMessage": "sampling",
    "elicitation/create": "elicitation",
}


class ColabSessionProxy:
    def __init__(self, transport):
        self.connected = asyncio.Event()
        self.browser: Peer | None = None
        self.browser_capabilities = {}
        self.runtime = RuntimeBridge()
        self.wss = ColabWebSocketServer(self._connect_browser, self._connect_runtime)
        self.server = ServerSession(
            transport,
            name="ColabMCP",
            version="1.0.1",
            capabilities={
                "tools": {"listChanged": True},
                "resources": {"listChanged": True, "subscribe": True},
                "prompts": {"listChanged": True},
                "logging": {},
                "completions": {},
            },
            on_request=self._request,
            on_notification=self._client_notification,
            instructions=(
                "Call open_colab_browser_connection to connect a Colab browser tab. "
                "Then use list_notebook_tools and call_notebook_tool to discover "
                "and use its notebook operations. "
                "Optional Chrome companion: runtime_status reports availability; "
                "runtime_action manages the runtime when it is connected. "
                "The user handles Google sign-in and any Colab permission prompts."
            ),
        )
        self._stack = AsyncExitStack()

    async def __aenter__(self):
        try:
            await self._stack.enter_async_context(self.wss)
            await self._stack.enter_async_context(self.server)
        except BaseException:
            await self._stack.aclose()
            raise
        return self

    async def __aexit__(self, *exc):
        await self._stack.aclose()

    async def _connect_browser(self, websocket):
        if not self.server.ready.is_set():
            await websocket.close(code=1013, reason="Initialize the MCP client first")
            return
        try:
            async with Peer(websocket, self._browser_request, self._browser_notification) as peer:
                capabilities = {
                    key: value for key, value in self.server.client_capabilities.items()
                    if key in CLIENT_METHOD_CAPABILITIES.values()
                }
                result = await initialize_client(peer, "ColabMCP", "1.0.1", capabilities)
                self.browser = peer
                self.browser_capabilities = result["capabilities"]
                self.connected.set()
                await self._catalog_changed()
                await peer.closed.wait()
        except (ConnectionError, RpcError, TimeoutError) as exc:
            logging.info("Colab connection ended: %s", exc)
        finally:
            was_connected = self.connected.is_set()
            self.browser = None
            self.browser_capabilities = {}
            self.connected.clear()
            await self.runtime.disconnect()
            if was_connected:
                await self._catalog_changed()

    async def _connect_runtime(self, websocket):
        if not self.connected.is_set():
            await websocket.close(code=1013, reason="Connect the Colab notebook first")
            return
        await self.runtime.connect(websocket)

    async def _catalog_changed(self):
        if self.server.peer.closed.is_set():
            return
        with suppress(ConnectionError):
            for kind in ("tools", "resources", "prompts"):
                await self.server.peer.notify(f"notifications/{kind}/list_changed")

    async def _browser_request(self, method, params):
        if method == "ping":
            return {}
        capability = CLIENT_METHOD_CAPABILITIES.get(method)
        if capability not in self.server.client_capabilities:
            raise RpcError(-32601, f"Client does not support {method}")
        return await self.server.peer.request(method, params)

    async def _browser_notification(self, method, params):
        if self.browser is not None and not self.server.peer.closed.is_set():
            await self.server.peer.notify(method, params)

    async def _client_notification(self, method, params):
        if self.browser is not None:
            with suppress(ConnectionError):
                await self.browser.notify(method, params)

    async def _request(self, method, params):
        if method == "tools/call" and params.get("name") in LOCAL_TOOLS:
            name = params["name"]
            arguments = params.get("arguments", {})
            schema = LOCAL_TOOLS[name]["inputSchema"]
            if (
                not isinstance(arguments, dict)
                or arguments.keys() - schema["properties"].keys()
                or set(schema.get("required", [])) - arguments.keys()
            ):
                raise RpcError(-32602, f"Invalid arguments for {name}")
            if name == CONNECTION_TOOL_NAME:
                return await self._open_browser(arguments.get("notebook_url"), params.get("_meta", {}))
            if name in (STATUS_TOOL, ACTION_TOOL):
                return await self.runtime.call(name, arguments)
            if name == NOTEBOOK_LIST_TOOL["name"]:
                if "cursor" in arguments and not isinstance(arguments["cursor"], str):
                    raise RpcError(-32602, "cursor must be a string")
                result = await self._forward("tools/list", arguments)
                return {
                    "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                    "structuredContent": result,
                    "isError": False,
                }
            if not isinstance(arguments["name"], str) or not isinstance(arguments["arguments"], dict):
                raise RpcError(-32602, "Expected a tool name and an arguments object")
            return await self._forward("tools/call", {**params, **arguments})
        result = await self._forward(method, params)
        if method == "tools/list":
            tools = result.get("tools")
            if not isinstance(tools, list) or any(
                not isinstance(tool, dict) or not isinstance(tool.get("name"), str)
                for tool in tools
            ):
                raise RpcError(-32603, "Colab returned an invalid tool list")
            tools = [tool for tool in tools if tool["name"] not in LOCAL_TOOLS]
            if not params.get("cursor"):
                tools = [*LOCAL_TOOLS.values(), *tools]
            return {**result, "tools": tools}
        return result

    async def _forward(self, method, params):
        capability = METHOD_CAPABILITIES.get(method)
        if capability is None:
            raise RpcError(-32601, f"Unknown method: {method}")
        browser = self.browser
        if browser is None or capability not in self.browser_capabilities:
            if method not in LIST_METHODS:
                raise RpcError(-32000, "Connect a Colab tab supporting this operation first")
            if params.get("cursor"):
                raise RpcError(
                    -32602, "Notebook connection changed; restart listing without a cursor"
                )
            return {LIST_METHODS[method]: []}
        return await browser.request(method, params)

    def connection_url(self, notebook_url=None):
        if notebook_url is None:
            notebook_url = COLAB + SCRATCH_PATH
        if not isinstance(notebook_url, str):
            raise RpcError(-32602, "notebook_url must be a string")
        try:
            url = urlsplit(notebook_url)
        except ValueError as exc:
            raise RpcError(-32602, "Invalid notebook URL") from exc
        if url.scheme != "https" or f"https://{url.netloc}" not in (COLAB, COLAB_ALT_DOMAIN):
            raise RpcError(
                -32602, "Use an HTTPS URL on colab.research.google.com or colab.google.com"
            )
        fragment = dict(parse_qsl(url.fragment))
        fragment.update(mcpProxyToken=self.wss.token, mcpProxyPort=str(self.wss.port))
        return urlunsplit(url._replace(fragment=urlencode(fragment)))

    async def _open_browser(self, notebook_url, metadata):
        url = self.connection_url(notebook_url)
        if not isinstance(metadata, dict):
            raise RpcError(-32602, "_meta must be an object")
        progress_token = metadata.get("progressToken")
        if progress_token is not None and type(progress_token) not in (str, int):
            raise RpcError(-32602, "Invalid progress token")
        if not self.connected.is_set():
            try:
                opened = await asyncio.to_thread(webbrowser.open_new, url)
            except webbrowser.Error:
                opened = False
            if opened:
                if progress_token is not None:
                    await self.server.peer.notify("notifications/progress", {
                        "progressToken": progress_token, "progress": 1, "total": 2,
                        "message": "Waiting for the user to connect in Colab",
                    })
                try:
                    await asyncio.wait_for(self.connected.wait(), UI_CONNECTION_TIMEOUT)
                except TimeoutError:
                    pass
        connected = self.connected.is_set()
        content = [{"type": "text", "text": "true" if connected else "false"}]
        if not connected:
            content.append({
                "type": "text",
                "text": f"Open this URL and allow the Colab connection: {url}",
            })
        if progress_token is not None:
            await self.server.peer.notify("notifications/progress", {
                "progressToken": progress_token, "progress": 2, "total": 2,
                "message": "Connected to Colab" if connected else "Colab is not connected",
            })
        return {"content": content, "structuredContent": {"result": connected}, "isError": False}
