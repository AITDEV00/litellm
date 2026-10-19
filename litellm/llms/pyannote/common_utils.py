import os
from typing import List, Optional

from litellm.llms.base_llm.base_utils import BaseLLMModelInfo
from litellm.types.utils import ProviderSpecificModelInfo


class PyannoteModelInfo(BaseLLMModelInfo):
    def get_provider_info(self, model: str) -> Optional[ProviderSpecificModelInfo]:
        return ProviderSpecificModelInfo(
            endpoint="/v1/diarize",
            mode="diarization",
        )

    def get_models(self, api_key=None, api_base=None) -> List[str]:
        return ["pyannote/community-1"]

    @staticmethod
    def get_api_key(api_key=None) -> Optional[str]:
        return api_key or os.environ.get("PYANNOTE_API_KEY")

    @staticmethod
    def get_api_base(api_base=None) -> Optional[str]:
        return api_base or os.environ.get("PYANNOTE_API_BASE")
