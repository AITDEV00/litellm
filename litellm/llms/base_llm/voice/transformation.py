import types
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Dict, Optional, Union

import httpx
from httpx import Headers

from litellm.llms.base_llm.text_to_speech.transformation import TextToSpeechRequestData

if TYPE_CHECKING:
    from litellm.litellm_core_utils.litellm_logging import Logging as _LiteLLMLoggingObj
    from litellm.llms.base_llm.chat.transformation import BaseLLMException as _BaseLLMException

    LiteLLMLoggingObj = _LiteLLMLoggingObj
    BaseLLMException = _BaseLLMException
else:
    LiteLLMLoggingObj = Any
    BaseLLMException = Any


class BaseVoiceConfig(ABC):
    def __init__(self):
        pass

    @classmethod
    def get_config(cls):
        return {
            k: v
            for k, v in cls.__dict__.items()
            if not k.startswith("__")
            and not k.startswith("_abc")
            and not isinstance(
                v,
                (
                    types.FunctionType,
                    types.BuiltinFunctionType,
                    classmethod,
                    staticmethod,
                ),
            )
            and v is not None
        }

    @abstractmethod
    def validate_environment(
        self,
        headers: dict,
        model: str,
        api_key: Optional[str] = None,
        api_base: Optional[str] = None,
    ) -> dict:
        pass

    @abstractmethod
    def get_complete_url(
        self,
        model: str,
        api_base: Optional[str],
        litellm_params: dict,
    ) -> str:
        pass

    @abstractmethod
    def transform_create_voice_request(
        self,
        model: str,
        voice_data: Dict[str, Any],
        optional_params: Dict,
        litellm_params: Dict,
        headers: dict,
    ) -> TextToSpeechRequestData:
        pass

    @abstractmethod
    def transform_create_voice_response(
        self,
        model: str,
        raw_response: httpx.Response,
        logging_obj: LiteLLMLoggingObj,
    ) -> Dict[str, Any]:
        pass

    def get_error_class(
        self, error_message: str, status_code: int, headers: Union[dict, Headers]
    ) -> "BaseLLMException":
        from litellm.llms.base_llm.chat.transformation import BaseLLMException

        raise BaseLLMException(
            status_code=status_code,
            message=error_message,
            headers=headers,
        )
