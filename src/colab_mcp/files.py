"""Bounded, background file transfers through the browser companion."""

import asyncio
import base64
import hashlib
import os
from pathlib import Path
import posixpath
import secrets
import tempfile

from minimcp import RpcError

from .results import tool_result


CHUNK_SIZE = 1024 * 1024
FILE_TOOLS = [
    {
        "name": name,
        "description": (
            description + " Supports arbitrary file types. Requires the Chrome companion "
            "and a connected runtime. Returns a transfer_id immediately; use "
            "transfer_status to check progress. File bytes do not enter the conversation. "
            "Only one transfer runs at a time. Existing files require overwrite=true."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "local_path": {"type": "string", "description": "Absolute local file path."},
                "colab_path": {"type": "string", "description": "Absolute runtime file path, e.g. /content/data.zip."},
                "overwrite": {"type": "boolean", "default": False},
            },
            "required": ["local_path", "colab_path"],
            "additionalProperties": False,
        },
        "annotations": {"destructiveHint": True},
    }
    for name, description in (
        ("upload_file", "Upload a local file to Colab, creating parent directories as needed."),
        ("download_file", "Download a Colab file to the local machine, creating parent directories as needed."),
    )
] + [
    {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": {"transfer_id": {"type": "string"}},
            "required": ["transfer_id"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": name == "transfer_status"},
    }
    for name, description in (
        ("transfer_status", "Get progress, completion, or error details for a file transfer. The last 20 transfers are retained."),
        ("cancel_transfer", "Cancel a file transfer and remove its incomplete temporary files where the connection is still available."),
    )
]
FILE_TOOL_NAMES = {tool["name"] for tool in FILE_TOOLS}


class FileTransfers:
    def __init__(self):
        self.jobs = {}
        self.task: asyncio.Task | None = None
        self.active_id = None

    @property
    def active(self):
        return self.task is not None and not self.task.done()

    async def close(self):
        if self.active:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    def call(self, name, arguments, peer):
        if name in ("transfer_status", "cancel_transfer"):
            transfer_id = arguments["transfer_id"]
            if not isinstance(transfer_id, str) or transfer_id not in self.jobs:
                raise RpcError(-32602, "Unknown transfer_id")
            job = self.jobs[transfer_id]
            if name == "cancel_transfer" and self.active and transfer_id == self.active_id:
                if job["state"] == "finalizing":
                    raise RpcError(-32000, "The complete file is being finalized; wait for transfer_status")
                if job["state"] == "running":
                    job["state"] = "cancelling"
                    self.task.cancel()
            return tool_result(dict(job), is_error=job["state"] == "failed")
        if self.active:
            raise RpcError(-32000, f"Transfer {self.active_id} is still running")
        if peer is None or peer.closed.is_set():
            raise RpcError(-32000, "Connect the optional Chrome companion before transferring files")
        local = arguments["local_path"]
        remote = arguments["colab_path"]
        overwrite = arguments.get("overwrite", False)
        if not isinstance(local, str) or not local or "\0" in local:
            raise RpcError(-32602, "local_path must be a file path")
        local = Path(local).expanduser()
        if not local.is_absolute():
            raise RpcError(-32602, "local_path must be absolute")
        if not isinstance(remote, str) or not remote.startswith("/") or "\0" in remote:
            raise RpcError(-32602, "colab_path must be an absolute Unix file path")
        remote = posixpath.normpath(remote)
        if remote == "/" or type(overwrite) is not bool:
            raise RpcError(-32602, "Expected a file destination and a boolean overwrite flag")
        if name == "upload_file" and not local.is_file():
            raise RpcError(-32602, "Local source is not a regular file")
        if name == "download_file" and local.exists() and not overwrite:
            raise RpcError(-32602, "Local destination exists; use overwrite=true to replace it")
        while len(self.jobs) >= 20:
            self.jobs.pop(next(iter(self.jobs)))
        transfer_id = secrets.token_hex(12)
        job = {
            "transfer_id": transfer_id,
            "direction": "upload" if name == "upload_file" else "download",
            "local_path": str(local), "colab_path": remote,
            "state": "running", "bytes_transferred": 0, "total_bytes": None,
        }
        self.jobs[transfer_id] = job
        self.active_id = transfer_id

        def mark_cancelled(task):
            # A task cancelled before its coroutine starts cannot run its cleanup.
            if task.cancelled():
                job["state"] = "cancelled"

        self.task = asyncio.create_task(self._run(peer, job, local, overwrite))
        self.task.add_done_callback(mark_cancelled)
        return tool_result(dict(job))

    async def _run(self, peer, job, local, overwrite):
        transfer_id = job["transfer_id"]
        temporary = None
        remote_finished = False

        async def request(method, **params):
            return await peer.request("files/" + method, {"transfer_id": transfer_id, **params}, timeout=120)

        try:
            if job["direction"] == "upload":
                with local.open("rb") as source:
                    initial = os.fstat(source.fileno())
                    job["total_bytes"] = initial.st_size
                    start = await request("begin", direction="upload", path=job["colab_path"],
                                          size=initial.st_size, overwrite=overwrite)
                    job["temporary_colab_path"] = start["temporary_path"]
                    digest = hashlib.sha256()
                    while job["bytes_transferred"] < initial.st_size:
                        data = source.read(min(CHUNK_SIZE, initial.st_size - job["bytes_transferred"]))
                        if not data:
                            raise OSError("Local file became shorter during upload")
                        await request("write", offset=job["bytes_transferred"],
                                      data=base64.b64encode(data).decode("ascii"))
                        digest.update(data)
                        job["bytes_transferred"] += len(data)
                    final = os.fstat(source.fileno())
                    if (initial.st_size, initial.st_mtime_ns) != (final.st_size, final.st_mtime_ns):
                        raise OSError("Local file changed during upload")
                    job["state"] = "finalizing"
                    result = await request("finish")
                    remote_finished = True
                    job.pop("temporary_colab_path", None)
                    if result.get("warning"):
                        job["warning"] = result["warning"]
            else:
                start = await request("begin", direction="download", path=job["colab_path"])
                size = start["size"]
                if type(size) is not int or size < 0:
                    raise ValueError("Colab returned an invalid file size")
                job["total_bytes"] = size
                local.parent.mkdir(parents=True, exist_ok=True)
                descriptor, path = tempfile.mkstemp(prefix=".colab-mcp-", suffix=".part", dir=local.parent)
                temporary = Path(path)
                digest = hashlib.sha256()
                with os.fdopen(descriptor, "wb") as destination:
                    while job["bytes_transferred"] < size:
                        length = min(CHUNK_SIZE, size - job["bytes_transferred"])
                        result = await request("read", offset=job["bytes_transferred"], length=length)
                        encoded = result["data"]
                        if not isinstance(encoded, str) or len(encoded) > 4 * ((length + 2) // 3):
                            raise ValueError("Colab returned an oversized file chunk")
                        data = base64.b64decode(encoded, validate=True)
                        if len(data) != length:
                            raise ValueError("Colab returned an incomplete file chunk")
                        destination.write(data)
                        digest.update(data)
                        job["bytes_transferred"] += len(data)
                job["state"] = "finalizing"
                await request("finish")
                remote_finished = True
                if overwrite:
                    temporary.replace(local)
                elif os.name == "nt":
                    # Windows rename refuses an existing destination, including on exFAT.
                    temporary.rename(local)
                else:
                    # A hard link commits without overwriting a file created during transfer.
                    os.link(temporary, local)
                    temporary.unlink()
                temporary = None
            job.update(state="completed", sha256=digest.hexdigest())
        except asyncio.CancelledError:
            job["state"] = "cancelled"
        except TimeoutError:
            job.update(state="failed", error="Colab did not respond to a file request within 120 seconds")
        except Exception as exc:
            job.update(state="failed", error=str(exc))
        finally:
            if not remote_finished:
                try:
                    await peer.request("files/abort", {"transfer_id": transfer_id}, timeout=10)
                    job.pop("temporary_colab_path", None)
                except Exception as exc:
                    job["cleanup_error"] = str(exc)
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError as exc:
                    job["cleanup_error"] = str(exc)
