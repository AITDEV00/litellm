import os
from typing import List, Optional

from litellm.llms.base_llm.base_utils import BaseLLMModelInfo
from litellm.types.utils import ProviderSpecificModelInfo


class HamsaModelInfo(BaseLLMModelInfo):
    def get_provider_info(self, model: str) -> Optional[ProviderSpecificModelInfo]:
        return ProviderSpecificModelInfo(
            endpoint="/v1/audio/transcriptions, /v1/realtime",
            mode="audio_transcription",
        )

    def get_models(self, api_key: Optional[str] = None, api_base: Optional[str] = None) -> List[str]:
        return []

    @staticmethod
    def get_api_key(api_key: Optional[str] = None) -> Optional[str]:
        return api_key or os.environ.get("HAMSA_API_KEY")

    @staticmethod
    def get_api_base(api_base: Optional[str] = None) -> Optional[str]:
        return api_base or os.environ.get("HAMSA_API_BASE")

    def validate_environment(
        self,
        headers: dict,
        model: str,
        messages: list,
        optional_params: dict,
        litellm_params: dict,
        api_key: Optional[str] = None,
        api_base: Optional[str] = None,
    ) -> dict:
        return headers

    @staticmethod
    def get_base_model(model: str) -> Optional[str]:
        return model
