import logging

from .client import FallbackClient

logger = logging.getLogger("oicm-discovery")


class FallbackReconciler:
    def __init__(self, client: FallbackClient):
        self.client = client

    async def reconcile(self) -> None:
        existing = await self.client.get_fallbacks()
        if not existing:
            return

        valid_names = await self.client.list_model_names()
        if not valid_names:
            return

        for model, targets in existing.items():
            if model not in valid_names:
                logger.info(
                    "Fallback for %s -> %s exists but model is not currently "
                    "registered (may be redeploying); keeping fallback config",
                    model,
                    targets,
                )
