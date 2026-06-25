"""OICM Discovery Controller — health check server."""

import asyncio
import logging
from aiohttp import web

logger = logging.getLogger("oicm-health")


async def health_handler(request: web.Request) -> web.Response:
    """Simple health check endpoint."""
    return web.json_response({"status": "healthy"})


def start_health_server(port: int = 8090):
    """Start a minimal HTTP server for k8s health checks."""
    app = web.Application()
    app.router.add_get("/health", health_handler)
    
    async def run():
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        logger.info(f"Health check server listening on :{port}")
    
    return run()
