# Colab MCP

A local stdio MCP server that connects an agent to a Colab browser tab.
Python 3.13+; the only runtime dependency is `websockets==17.4`.

Run from the checkout without installing the project or creating a virtual environment:

```powershell
# Skip this if websockets is already installed.
python -m pip install --only-binary=:all: websockets==17.4
cd src
python -m colab_mcp
```

The server waits for MCP messages on stdin. Normally your MCP client launches it.
For Codex, use this configuration with your actual checkout path and Python executable:

```toml
[mcp_servers.colab]
command = "python"
args = ["-m", "colab_mcp"]
cwd = 'D:\path\to\colab-mcp\src'
tool_timeout_sec = 120
```

Call `open_colab_browser_connection` to open a scratch notebook. Its optional
`notebook_url` argument opens an existing HTTPS Colab notebook. Sign in and accept
Colab's connection prompt in the browser. Notebook tools appear after connection;
the MCP client must handle `notifications/tools/list_changed`. Keep the tab open.
If the browser cannot open or connection times out, the tool returns a manual URL.

`src/minimcp` contains the independent, standard-library-only protocol code:
JSON-RPC, initialization, notifications, cancellation, and stdio. It supports MCP
2025-11-25, 2025-06-18, and 2024-11-05; it does not implement HTTP/OAuth or task
extensions. `src/colab_mcp` handles browser authentication and forwards tools,
resources, prompts, progress, and supported client callbacks.

Logs go to stderr, or to a directory supplied with `--log`. The WebSocket listener
binds to `127.0.0.1`, accepts one tab, and checks its token and Colab origin.

Run checks from the repository root with `python -m pytest`. Tests need only pytest
and websockets; they do not open a real browser or contact Google.

Building an installable wheel uses Hatchling, declared in `pyproject.toml`.
Running from `src` does not load or require that build backend.
