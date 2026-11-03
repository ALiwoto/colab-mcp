# Copyright 2026 Google Inc.
# SPDX-License-Identifier: Apache-2.0

import asyncio
from collections.abc import Awaitable, Callable
import logging
import secrets
from urllib.parse import parse_qs, urlsplit

from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import MultipleValuesError

from minimcp.stdio import MAX_MESSAGE_BYTES

COLAB = "https://colab.research.google.com"
COLAB_ALT_DOMAIN = "https://colab.google.com"
SCRATCH_PATH = "/notebooks/empty.ipynb"


class ColabWebSocketServer:
    """A Colab tab and optional companion, listening only on IPv4 loopback."""

    def __init__(
        self,
        on_connection: Callable[[ServerConnection], Awaitable[None]],
        on_runtime_connection: Callable[[ServerConnection], Awaitable[None]] | None = None,
    ):
        self.port = 0
        self.token = secrets.token_urlsafe(32)
        self._on_connection = on_connection
        self._connection_lock = asyncio.Lock()
        self._on_runtime_connection = on_runtime_connection
        self._runtime_lock = asyncio.Lock()
        self._server = None

    def _authorize(self, connection, request):
        try:
            tokens = parse_qs(urlsplit(request.path).query).get("access_token", [])
            authorization = request.headers.get("Authorization")
        except (ValueError, MultipleValuesError):
            return connection.respond(400, "Malformed authorization")
        if tokens:
            if len(tokens) != 1:
                return connection.respond(400, "Provide one access token")
            token = tokens[0]
        elif authorization:
            parts = authorization.split()
            if len(parts) != 2 or parts[0].lower() != "bearer":
                return connection.respond(400, "Expected a Bearer token")
            token = parts[1]
        else:
            return connection.respond(401, "Authorization required")
        if not secrets.compare_digest(token.encode("utf-8"), self.token.encode("ascii")):
            return connection.respond(403, "Invalid access token")
        return None

    async def _handle_connection(self, websocket):
        callback = self._on_connection
        lock = self._connection_lock
        if urlsplit(websocket.request.path).path == "/runtime":
            callback = self._on_runtime_connection
            lock = self._runtime_lock
        if callback is None:
            await websocket.close(code=1008, reason="Runtime companion is not enabled")
            return
        try:
            # A tab handoff may arrive while the previous channel is still closing.
            await asyncio.wait_for(lock.acquire(), timeout=5)
        except TimeoutError:
            await websocket.close(code=1013, reason="This Colab channel is already connected")
            return
        try:
            await callback(websocket)
        finally:
            lock.release()

    async def __aenter__(self):
        self._server = await serve(
            self._handle_connection,
            "127.0.0.1",
            0,
            subprotocols=["mcp"],
            origins=[COLAB, COLAB_ALT_DOMAIN],
            process_request=self._authorize,
            max_size=MAX_MESSAGE_BYTES,
            close_timeout=3,
        )
        self.port = self._server.sockets[0].getsockname()[1]
        logging.info("Colab bridge listening on 127.0.0.1:%s", self.port)
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()
