"""Bidirectional JSON-RPC for MCP. A transport sends and iterates JSON messages."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
import contextlib
import json
import logging
from typing import Any, Protocol

type Object = dict[str, Any]
type RequestHandler = Callable[[str, Object], Awaitable[Object]]
type NotificationHandler = Callable[[str, Object], Awaitable[None]]

logger = logging.getLogger(__name__)


class Transport(Protocol):
    def __aiter__(self) -> AsyncIterator[str | bytes]: ...

    async def send(self, message: str) -> None: ...


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data

    def as_dict(self) -> Object:
        error = {"code": self.code, "message": str(self)}
        if self.data is not None:
            error["data"] = self.data
        return error


def _is_id(value: Any) -> bool:
    return type(value) in (str, int)


def _reject_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")


class Peer:
    """One connection, with independent incoming and outgoing request IDs."""

    def __init__(
        self,
        transport: Transport,
        on_request: RequestHandler,
        on_notification: NotificationHandler | None = None,
    ):
        self.transport = transport
        self.on_request = on_request
        self.on_notification = on_notification
        self.closed = asyncio.Event()
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Object]] = {}
        self._incoming: dict[str | int, tuple[str, asyncio.Task]] = {}
        self._send_lock = asyncio.Lock()
        self._reader: asyncio.Task | None = None

    async def __aenter__(self):
        self._reader = asyncio.create_task(self._read())
        return self

    async def __aexit__(self, *exc):
        self._reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._reader

    async def _send(self, message: Object) -> None:
        encoded = json.dumps(
            message, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )
        async with self._send_lock:
            if self.closed.is_set():
                raise ConnectionError("MCP connection closed")
            try:
                await self.transport.send(encoded)
            except Exception as exc:
                raise ConnectionError("MCP transport send failed") from exc

    async def notify(self, method: str, params: Object | None = None) -> None:
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        await self._send(message)

    async def request(
        self, method: str, params: Object | None = None, *, timeout: float | None = None
    ) -> Object:
        request_id = self._next_id
        self._next_id += 1
        response = asyncio.get_running_loop().create_future()
        self._pending[request_id] = response
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        sent = False
        try:
            await self._send(message)
            sent = True
            async with asyncio.timeout(timeout):
                return await response
        except (asyncio.CancelledError, TimeoutError):
            if sent and method != "initialize":
                with contextlib.suppress(Exception):
                    await self.notify("notifications/cancelled", {"requestId": request_id})
            raise
        finally:
            self._pending.pop(request_id, None)
            response.cancel()

    async def _error(self, request_id, code: int, message: str) -> None:
        await self._send({
            "jsonrpc": "2.0", "id": request_id,
            "error": {"code": code, "message": message},
        })

    async def _read(self) -> None:
        try:
            async for raw in self.transport:
                try:
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8")
                    message = json.loads(raw, parse_constant=_reject_constant)
                except (ValueError, UnicodeError):
                    await self._error(None, -32700, "Invalid JSON")
                    continue
                await self._receive(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.info("MCP transport closed: %s", exc)
        finally:
            self.closed.set()
            for response in self._pending.values():
                if not response.done():
                    response.set_exception(ConnectionError("MCP connection closed"))
            tasks = [task for _, task in self._incoming.values()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _receive(self, message: Any) -> None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            await self._error(None, -32600, "Invalid JSON-RPC message")
            return
        request_id = message.get("id")
        if "method" not in message:
            if "result" in message or "error" in message:
                self._receive_response(message)
            else:
                await self._error(None, -32600, "Expected a request, notification, or response")
            return
        method = message["method"]
        is_request = "id" in message
        if (
            not isinstance(method, str)
            or "result" in message or "error" in message
            or (is_request and not _is_id(request_id))
        ):
            await self._error(None, -32600, "Invalid JSON-RPC request")
            return
        params = message.get("params", {})
        if not isinstance(params, dict):
            if is_request:
                await self._error(request_id, -32602, "MCP parameters must be an object")
            return
        if not is_request:
            if method == "notifications/cancelled":
                target = params.get("requestId")
                running = self._incoming.get(target) if _is_id(target) else None
                if running and running[0] != "initialize":
                    running[1].cancel()
            elif self.on_notification:
                try:
                    await self.on_notification(method, params)
                except Exception:
                    logger.exception("MCP notification failed: %s", method)
            return
        if request_id in self._incoming:
            await self._error(request_id, -32600, "Duplicate active request ID")
        elif method == "initialize":
            # Establish state before reading a following initialized notification.
            await self._dispatch(request_id, method, params)
        else:
            task = asyncio.create_task(self._dispatch(request_id, method, params))
            self._incoming[request_id] = (method, task)
            task.add_done_callback(lambda task: self._incoming.pop(request_id, None))

    def _receive_response(self, message: Object) -> None:
        request_id = message.get("id")
        if not _is_id(request_id):
            return
        response = self._pending.get(request_id)
        if response is None or response.done():
            return
        error = message.get("error")
        if "result" in message and "error" not in message and isinstance(message["result"], dict):
            response.set_result(message["result"])
        elif (
            "result" not in message and isinstance(error, dict)
            and type(error.get("code")) is int and isinstance(error.get("message"), str)
        ):
            response.set_exception(RpcError(error["code"], error["message"], error.get("data")))
        else:
            response.set_exception(RpcError(-32603, "Invalid MCP response"))

    async def _dispatch(self, request_id, method: str, params: Object) -> None:
        try:
            try:
                result = await self.on_request(method, params)
                reply = {"result": result}
            except RpcError as exc:
                reply = {"error": exc.as_dict()}
            except ConnectionError as exc:
                reply = {"error": {"code": -32000, "message": str(exc)}}
            except Exception:
                logger.exception("MCP request failed: %s", method)
                reply = {"error": {"code": -32603, "message": "Internal server error"}}
            await self._send({"jsonrpc": "2.0", "id": request_id, **reply})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.info("Could not send MCP response: %s", exc)
