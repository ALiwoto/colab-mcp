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
    """One authenticated Colab tab, listening only on IPv4 loopback."""

    def __init__(self, on_connection: Callable[[ServerConnection], Awaitable[None]]):
        self.port = 0
        self.token = secrets.token_urlsafe(32)
        self._on_connection = on_connection
        self._connection_lock = asyncio.Lock()
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
        if self._connection_lock.locked():
            await websocket.close(code=1013, reason="A Colab tab is already connected")
            return
        async with self._connection_lock:
            await self._on_connection(websocket)

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
