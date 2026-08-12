import asyncio
import logging
import signal
import sys

from . import config as _config  # noqa: F401 - side-effect: configures logging
from .controller import DiscoveryController

logger = logging.getLogger("oicm-discovery")


def run_once():
    controller = DiscoveryController()
    asyncio.run(controller.full_sync())


def run():
    controller = DiscoveryController()
    loop = asyncio.new_event_loop()

    def _shutdown(signum, _frame):
        logger.info(f"Received signal {signum}, shutting down...")
        loop.create_task(controller.stop())

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        loop.run_until_complete(controller.start())
    except KeyboardInterrupt:
        pass
    finally:
        loop.run_until_complete(controller.stop())
        loop.close()


if __name__ == "__main__":
    if "--once" in sys.argv:
        run_once()
    else:
        run()
