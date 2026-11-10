"""Run the service with bounded shutdown and secret-free diagnostics."""

import asyncio
import logging
import os
import signal

from rpt_central_auth.config import load_settings
from rpt_central_auth.server import Application


async def serve():
    app = Application(load_settings())
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stopped.set)
    try:
        await app.start()
        logging.info("credential service ready")
        await stopped.wait()
    finally:
        clean = await app.stop()
        if not clean:
            # A trusted plugin ignored its timeout. Python cannot terminate its thread;
            # exit the process after the agreed drain budget instead of waiting forever.
            logging.error("backend did not close within shutdown budget")
            os._exit(1)


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        asyncio.run(serve())
    except Exception:
        logging.error(
            "credential service startup or shutdown failed; check configuration and backend"
        )
        raise SystemExit(1) from None
