"""Optional runtime controls supplied by the Chrome companion."""

import asyncio

from minimcp import Peer, RpcError

from .results import tool_result


STATUS_TOOL = "runtime_status"
ACTION_TOOL = "runtime_action"
ACTIONS = ("connect", "disconnect", "restart", "interrupt", "delete", "configure")
RUNTIME_TOOLS = [
    {
        "name": STATUS_TOOL,
        "description": (
            "Report whether the optional Chrome companion is connected, the current "
            "Colab runtime state and settings, and the last runtime operation. "
            "Use this to check completion after runtime_action."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": ACTION_TOOL,
        "description": (
            "Manage this notebook's runtime using the optional Chrome companion. "
            "Returns immediately; check runtime_status for completion or errors. "
            "Connect uses the notebook's selected runtime. Disconnect detaches the tab; "
            "delete releases its runtime and loses its files and memory. Restart clears "
            "kernel memory. Configure changes runtime settings and may replace a running "
            "runtime. GPU/TPU and high-memory availability depend on the user's account. "
            "The user handles any Colab permission or confirmation dialogs."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(ACTIONS)},
                "accelerator": {
                    "type": "string", "enum": ["CPU", "GPU", "TPU"],
                    "description": "Configure only. Required when changing runtime settings.",
                },
                "accelerator_model": {
                    "type": "string",
                    "description": "Configure only. Colab's model identifier, e.g. T4; empty uses its default.",
                },
                "high_memory": {"type": "boolean", "description": "Configure only; omitted preserves the setting."},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        "annotations": {"destructiveHint": True, "openWorldHint": True},
    },
]


class RuntimeBridge:
    def __init__(self):
        self.peer: Peer | None = None
        self.ready = asyncio.Event()

    async def connect(self, websocket):
        async def on_request(method, params):
            if method == "ping":
                return {}
            raise RpcError(-32601, f"Unknown companion method: {method}")

        async with Peer(websocket, on_request) as peer:
            self.peer = peer
            self.ready.set()
            try:
                await peer.closed.wait()
            finally:
                self.peer = None
                self.ready.clear()

    async def disconnect(self):
        peer = self.peer
        self.peer = None
        self.ready.clear()
        if peer is not None:
            await peer.transport.close(code=1000, reason="Notebook disconnected")

    async def call(self, name, arguments):
        if name == ACTION_TOOL:
            action = arguments["action"]
            if action not in ACTIONS:
                raise RpcError(-32602, "Unknown runtime action")
            if action == "configure":
                if arguments.get("accelerator") not in ("CPU", "GPU", "TPU"):
                    raise RpcError(-32602, "Configure requires accelerator: CPU, GPU, or TPU")
                if "accelerator_model" in arguments and not isinstance(arguments["accelerator_model"], str):
                    raise RpcError(-32602, "accelerator_model must be a string")
                if arguments["accelerator"] == "CPU" and arguments.get("accelerator_model"):
                    raise RpcError(-32602, "CPU does not accept accelerator_model")
                if "high_memory" in arguments and type(arguments["high_memory"]) is not bool:
                    raise RpcError(-32602, "high_memory must be a boolean")
            elif arguments.keys() != {"action"}:
                raise RpcError(-32602, "Runtime settings are only accepted for configure")

        peer = self.peer
        if peer is None or peer.closed.is_set():
            result = {
                "extension_connected": False,
                "message": (
                    "Load the chrome-extension folder as an unpacked extension, then "
                    "reload the Colab tab and allow its MCP connection. "
                    "Notebook tools work without this optional extension."
                ),
            }
            is_error = name == ACTION_TOOL
        else:
            method = "runtime/status" if name == STATUS_TOOL else "runtime/action"
            try:
                result = await peer.request(method, arguments, timeout=10)
                is_error = False
            except ConnectionError:
                result = {"extension_connected": False, "message": "The Chrome companion disconnected."}
                is_error = name == ACTION_TOOL
            except TimeoutError:
                result = {"message": "The Chrome companion did not respond. Check the Colab tab before retrying."}
                is_error = True
            except RpcError as exc:
                result = {"message": str(exc)}
                is_error = True
        return tool_result(result, is_error=is_error)
