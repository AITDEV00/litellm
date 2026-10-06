from abc import ABC, abstractmethod
from typing import Dict

import httpx

from ..models import OicmModel, parse_model_list


async def probe_v1_models(client: httpx.AsyncClient, url: str) -> list[str]:
    """List the served model ids at an OpenAI-compatible ``/v1/models``.

    Shared by both discovery sources: the probe and the 405 handling are the
    same whether the address is an in-cluster Service or a Submariner globalnet
    IP. A 405 means the server does not expose the endpoint at all (a native
    REST surface), which is a normal answer rather than a failure, so it yields
    an empty list instead of raising.
    """
    resp = await client.get(url)
    if resp.status_code == 405:
        return []
    resp.raise_for_status()
    return parse_model_list(resp.json())


class ModelSource(ABC):
    @abstractmethod
    async def discover(self) -> Dict[str, OicmModel]:
        ...

    async def aclose(self) -> None:
        """Release transport resources. Default no-op for stateless sources."""
        return
