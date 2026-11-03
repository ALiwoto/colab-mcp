# Copyright 2026 Google Inc.
# SPDX-License-Identifier: Apache-2.0

import argparse
import asyncio
import datetime
import logging
from pathlib import Path

from minimcp.stdio import StdioTransport

from .session import ColabSessionProxy


async def main_async():
    parser = argparse.ArgumentParser(
        description="Connect a local MCP client to a Colab browser tab."
    )
    parser.add_argument(
        "-l", "--log", type=Path, help="Directory for log files; defaults to stderr."
    )
    args = parser.parse_args()
    logging_options = {"level": logging.INFO, "format": "%(asctime)s %(levelname)s %(message)s"}
    if args.log:
        args.log.mkdir(parents=True, exist_ok=True)
        filename = datetime.datetime.now().strftime("colab-mcp.%Y-%m-%d_%H-%M-%S.log")
        logging_options["filename"] = args.log / filename
    logging.basicConfig(**logging_options)
    async with StdioTransport() as transport:
        async with ColabSessionProxy(transport) as proxy:
            await proxy.server.peer.closed.wait()


def main():
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass
