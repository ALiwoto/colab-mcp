"""MCP initialization around JSON-RPC; application methods stay with the caller."""

import asyncio

from .rpc import NotificationHandler, Object, Peer, RequestHandler, RpcError, Transport

# The implemented subset predates task extensions. Never advertise task support.
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2024-11-05")


async def initialize_client(
    peer: Peer, name: str, version: str, capabilities: Object
) -> Object:
    result = await peer.request(
        "initialize",
        {
            "protocolVersion": PROTOCOL_VERSIONS[0],
            "clientInfo": {"name": name, "version": version},
            "capabilities": capabilities,
        },
        timeout=30,
    )
    if result.get("protocolVersion") not in PROTOCOL_VERSIONS:
        raise RpcError(-32602, "Unsupported MCP protocol version from server")
    if not isinstance(result.get("capabilities"), dict):
        raise RpcError(-32602, "Missing MCP server capabilities")
    await peer.notify("notifications/initialized")
    return result


class ServerSession:
    def __init__(
        self,
        transport: Transport,
        *,
        name: str,
        version: str,
        capabilities: Object,
        on_request: RequestHandler,
        on_notification: NotificationHandler | None = None,
        instructions: str = "",
    ):
        self.peer = Peer(transport, self._request, self._notification)
        self.ready = asyncio.Event()
        self.client_capabilities: Object = {}
        self.protocol_version: str | None = None
        self._info = {"name": name, "version": version}
        self._capabilities = capabilities
        self._instructions = instructions
        self._on_request = on_request
        self._on_notification = on_notification

    async def __aenter__(self):
        await self.peer.__aenter__()
        return self

    async def __aexit__(self, *exc):
        await self.peer.__aexit__(*exc)

    async def _request(self, method: str, params: Object) -> Object:
        if method == "ping":
            return {}
        if method == "initialize":
            if self.protocol_version is not None:
                raise RpcError(-32600, "Session is already initialized")
            version = params.get("protocolVersion")
            capabilities = params.get("capabilities")
            info = params.get("clientInfo")
            if (
                not isinstance(version, str) or not isinstance(capabilities, dict)
                or not isinstance(info, dict) or not isinstance(info.get("name"), str)
                or not isinstance(info.get("version"), str)
            ):
                raise RpcError(-32602, "Invalid MCP initialization parameters")
            self.client_capabilities = capabilities
            self.protocol_version = (
                version if version in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            )
            return {
                "protocolVersion": self.protocol_version,
                "serverInfo": self._info,
                "capabilities": self._capabilities,
                "instructions": self._instructions,
            }
        if not self.ready.is_set():
            raise RpcError(-32000, "Complete MCP initialization before calling methods")
        return await self._on_request(method, params)

    async def _notification(self, method: str, params: Object) -> None:
        if method == "notifications/initialized":
            if self.protocol_version is not None:
                self.ready.set()
        elif self.ready.is_set() and self._on_notification:
            await self._on_notification(method, params)
