"""pyannoteAI-compatible diarization config for coro-served deployments.

The contract is the pyannoteAI cloud ``POST /v1/diarize``: multipart ``file``
upload (coro's transport delta from the cloud's JSON+url shape), form fields
forwarded verbatim, and the pod's ``DiarizationJob`` JSON returned as-is.
Unsupported fields are the pod's own per-field 400s (the vendor contract),
so nothing here validates or translates request semantics. The serving model
id on ADEO deployments is ``pyannote/community-1``
(``CORO_PYANNOTE_DIARIZE_MODEL_ID``), and the pod rejects the spec enum
default ``community-1`` there, so the ``model`` field must always be sent.

Model-id handling: the router merges the deployment's ``litellm_params.model``
into the call, which is the controller-registered double-prefixed string
(``pyannote/pyannote/community-1``). The pod only accepts the serving id
(``pyannote/community-1``), so the leading provider prefix is stripped here,
at the last point before the wire. This is the only place model-string
shape is touched; everything upstream sees the registered string.
"""

from typing import Any, Dict, Optional, Tuple

import httpx

from litellm.llms.base_llm.diarization.transformation import (
    BaseDiarizationConfig,
    DiarizationRequestData,
)
from litellm.litellm_core_utils.litellm_logging import Logging as LiteLLMLoggingObj


class PyannoteDiarizationConfig(BaseDiarizationConfig):
    DIARIZE_ENDPOINT_PATH: str = "/v1/diarize"
    PROVIDER_PREFIX: str = "pyannote/"

    def validate_environment(
        self,
        headers: dict,
        model: str,
        api_key: Optional[str] = None,
        api_base: Optional[str] = None,
    ) -> dict:
        return headers

    def get_complete_url(
        self,
        model: str,
        api_base: Optional[str],
        litellm_params: dict,
    ) -> str:
        if api_base is None:
            raise ValueError(
                "Missing pyannote API base. Set api_base in the model config "
                "(the discovery controller stamps the coro pod's service URL)."
            )
        return f"{api_base.rstrip('/')}{self.DIARIZE_ENDPOINT_PATH}"

    def _serving_model_id(self, model: str) -> str:
        while model.startswith(self.PROVIDER_PREFIX):
            model = model[len(self.PROVIDER_PREFIX):]
        return f"{self.PROVIDER_PREFIX}{model}"

    def transform_diarization_request(
        self,
        model: str,
        form_fields: Dict[str, Any],
        audio_file: Tuple[str, bytes, str],
        optional_params: Dict,
        litellm_params: Dict,
    ) -> DiarizationRequestData:
        form_data = dict(form_fields)
        form_data["model"] = self._serving_model_id(model)
        return DiarizationRequestData(
            form_data=form_data,
            files={"file": audio_file},
        )

    def transform_diarization_response(
        self,
        model: str,
        raw_response: httpx.Response,
        logging_obj: LiteLLMLoggingObj,
    ) -> Dict[str, Any]:
        return raw_response.json()
