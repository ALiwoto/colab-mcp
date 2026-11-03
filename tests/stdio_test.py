import asyncio
import json
import os
from pathlib import Path
import sys


def test_stdio_process_and_shutdown():
    async def scenario():
        source = str(Path(__file__).resolve().parents[1] / "src")
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "colab_mcp",
            env={**os.environ, "PYTHONPATH": source},
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        async def exchange(message):
            process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
            await process.stdin.drain()
            return json.loads(await asyncio.wait_for(process.stdout.readline(), 5))

        try:
            response = await exchange({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25", "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            })
            assert response["result"]["capabilities"]["tools"]["listChanged"] is True
            process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
            response = await exchange({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            assert response["result"]["tools"][0]["name"] == "open_colab_browser_connection"
            response = await exchange({
                "jsonrpc": "2.0", "id": 3, "method": "ping", "params": {"text": "فارسی" * 30000},
            })
            assert response["result"] == {}
            process.stdin.write(b'\xff\n')
            await process.stdin.drain()
            error = json.loads(await asyncio.wait_for(process.stdout.readline(), 5))
            assert error["error"]["code"] == -32700
            process.stdin.close()
            assert await asyncio.wait_for(process.wait(), 5) == 0
            assert await process.stdout.read() == b""
            assert b"Traceback" not in await process.stderr.read()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    asyncio.run(scenario())
