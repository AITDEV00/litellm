from abc import ABC, abstractmethod
from typing import Dict

from ..models import OicmModel


class ModelSource(ABC):
    @abstractmethod
    async def discover(self) -> Dict[str, OicmModel]:
        ...
