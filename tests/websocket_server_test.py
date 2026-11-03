# Copyright 2026 Google Inc.
# SPDX-License-Identifier: Apache-2.0

import asyncio

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from colab_mcp.websocket_server import COLAB, COLAB_ALT_DOMAIN, ColabWebSocketServer


def test_authenticated_exchange_and_single_connection():
    async def scenario():
        released = asyncio.Event()

        async def echo(socket):
            try:
                async for message in socket:
                    await socket.send(message)
            finally:
                released.set()

        async with ColabWebSocketServer(echo) as server:
            uri = f"ws://127.0.0.1:{server.port}"
            async with connect(
                uri, origin=COLAB, subprotocols=["mcp"], proxy=None,
                additional_headers={"Authorization": f"Bearer {server.token}"},
            ) as first:
                await first.send("test UTF-8: فارسی 🐍")
                assert await first.recv() == "test UTF-8: فارسی 🐍"
                async with connect(
                    uri + f"?access_token={server.token}",
                    origin=COLAB_ALT_DOMAIN, subprotocols=["mcp"], proxy=None,
                ) as second:
                    with pytest.raises(ConnectionClosed) as error:
                        await second.recv()
                    assert error.value.rcvd.code == 1013
                await first.send("still connected")
                assert await first.recv() == "still connected"
            await asyncio.wait_for(released.wait(), 5)
            async with connect(
                uri + f"?access_token={server.token}",
                origin=COLAB_ALT_DOMAIN, subprotocols=["mcp"], proxy=None,
            ) as replacement:
                await replacement.send("new tab")
                assert await replacement.recv() == "new tab"

    asyncio.run(scenario())


@pytest.mark.parametrize("origin,query,authorization,status", [
    ("https://wrong.example", "?access_token={token}", None, 403),
    (None, "?access_token={token}", None, 403),
    (COLAB, "", None, 401),
    (COLAB, "", "Bearer wrong", 403),
    (COLAB, "", "Bearer", 400),
    (COLAB, "", "Basic {token}", 400),
    (COLAB, "?access_token={token}suffix", None, 403),
    (COLAB, "?other=access_token={token}", None, 401),
    (COLAB, "/access_token={token}", None, 401),
    (COLAB, "?access_token={token}&access_token=wrong", None, 400),
])
def test_authentication_boundary(origin, query, authorization, status):
    async def scenario():
        async def unexpected_connection(socket):
            pytest.fail("An unauthorized client reached the application")

        async with ColabWebSocketServer(unexpected_connection) as server:
            headers = {}
            if authorization:
                headers["Authorization"] = authorization.format(token=server.token)
            with pytest.raises(InvalidStatus) as error:
                async with connect(
                    f"ws://127.0.0.1:{server.port}" + query.format(token=server.token),
                    origin=origin, subprotocols=["mcp"], additional_headers=headers, proxy=None,
                ):
                    pass
            assert error.value.response.status_code == status

    asyncio.run(scenario())
