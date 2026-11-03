"""UTF-8, newline-delimited stdio without platform-specific dependencies."""

import asyncio
from concurrent.futures import CancelledError
import contextlib
import os
import threading

MAX_MESSAGE_BYTES = 16 * 1024 * 1024


class StdioTransport:
    def __init__(self, input_fd: int = 0, output_fd: int = 1):
        self.input_fd = input_fd
        self.output_fd = output_fd
        self._chunks = asyncio.Queue(maxsize=1)
        self._stopped = threading.Event()
        self._delivery = None

    async def __aenter__(self):
        loop = asyncio.get_running_loop()
        # A daemon reading an OS descriptor cannot hold Python's buffered stdin
        # lock or keep shutdown waiting on an executor thread after Ctrl+C.
        threading.Thread(target=self._read, args=(loop,), daemon=True).start()
        return self

    async def __aexit__(self, *exc):
        self._stopped.set()
        if self._delivery is not None:
            self._delivery.cancel()

    def _read(self, loop) -> None:
        while not self._stopped.is_set():
            try:
                chunk = os.read(self.input_fd, 65536)
            except OSError as exc:
                chunk = exc
            if self._stopped.is_set():
                return
            delivery = asyncio.run_coroutine_threadsafe(self._chunks.put(chunk), loop)
            self._delivery = delivery
            if self._stopped.is_set():
                delivery.cancel()
                return
            with contextlib.suppress(CancelledError):
                delivery.result()
            if not chunk or isinstance(chunk, Exception):
                return

    async def __aiter__(self):
        buffer = bytearray()
        while True:
            chunk = await self._chunks.get()
            if isinstance(chunk, Exception):
                raise chunk
            if not chunk:
                if buffer:
                    yield bytes(buffer)
                return
            buffer.extend(chunk)
            while (end := buffer.find(b"\n")) != -1:
                if end > MAX_MESSAGE_BYTES:
                    raise ValueError("MCP message exceeds 16 MiB")
                line = bytes(buffer[:end])
                del buffer[:end + 1]
                if line.strip():
                    yield line
            if len(buffer) > MAX_MESSAGE_BYTES:
                raise ValueError("MCP message exceeds 16 MiB")

    async def send(self, message: str) -> None:
        await asyncio.to_thread(self._write, (message + "\n").encode("utf-8"))

    def _write(self, data: bytes) -> None:
        remaining = memoryview(data)
        while remaining:
            written = os.write(self.output_fd, remaining)
            remaining = remaining[written:]
