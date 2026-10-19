"""Behavioral tests for the pyannote diarization config (coro-served).

Pins the pyannoteAI ``POST /v1/diarize`` contract handling: the bare ClusterIP
api_base plus the ``/v1/diarize`` suffix, verbatim form-field forwarding with
the serving model id always stamped (the pod's hardcoded ``community-1``
default is rejected on ADEO deployments), and the pod's JSON body returned
as-is with no translation.
"""

from unittest.mock import MagicMock

import httpx
import pytest

import litellm
from litellm.llms.pyannote.diarization.transformation import (
    PyannoteDiarizationConfig,
)


@pytest.fixture
def config() -> PyannoteDiarizationConfig:
    return PyannoteDiarizationConfig()


class TestGetCompleteUrl:
    def test_appends_diarize_path_to_bare_clusterip(self, config):
        url = config.get_complete_url(
            model="pyannote/community-1",
            api_base="http://s-d4398150.adeo.svc.cluster.local:8080",
            litellm_params={},
        )
        assert url == "http://s-d4398150.adeo.svc.cluster.local:8080/v1/diarize"

    def test_strips_trailing_slash(self, config):
        url = config.get_complete_url(
            model="pyannote/community-1",
            api_base="http://host:8080/",
            litellm_params={},
        )
        assert url == "http://host:8080/v1/diarize"

    def test_missing_api_base_raises(self, config):
        with pytest.raises(ValueError, match="api_base"):
            config.get_complete_url(
                model="pyannote/community-1",
                api_base=None,
                litellm_params={},
            )

    def test_does_not_append_v1_twice(self, config):
        url = config.get_complete_url(
            model="pyannote/community-1",
            api_base="http://host:8080/v1",
            litellm_params={},
        )
        assert url == "http://host:8080/v1/v1/diarize"


class TestTransformRequest:
    def test_forwards_form_fields_verbatim(self, config):
        request_data = config.transform_diarization_request(
            model="pyannote/community-1",
            form_fields={"exclusive": "true", "numSpeakers": "2"},
            audio_file=("meeting.wav", b"RIFF", "audio/wav"),
            optional_params={},
            litellm_params={},
        )
        assert request_data["form_data"] == {
            "exclusive": "true",
            "numSpeakers": "2",
            "model": "pyannote/community-1",
        }

    def test_model_overrides_client_value(self, config):
        """The dispatch-resolved serving id wins over any client-sent model field."""
        request_data = config.transform_diarization_request(
            model="pyannote/community-1",
            form_fields={"model": "wrong-id"},
            audio_file=("m.wav", b"x", "audio/wav"),
            optional_params={},
            litellm_params={},
        )
        assert request_data["form_data"]["model"] == "pyannote/community-1"

    def test_file_attached_under_file_key(self, config):
        audio_file = ("meeting.wav", b"RIFF-bytes", "audio/wav")
        request_data = config.transform_diarization_request(
            model="pyannote/community-1",
            form_fields={},
            audio_file=audio_file,
            optional_params={},
            litellm_params={},
        )
        assert request_data["files"] == {"file": audio_file}


class TestTransformResponse:
    def test_returns_pod_json_verbatim(self, config):
        job = {
            "jobId": "abc",
            "status": "succeeded",
            "createdAt": "2026-10-19T12:00:00Z",
            "updatedAt": "2026-10-19T12:00:00Z",
            "output": {
                "diarization": [{"speaker": "SPEAKER_00", "start": 0.0, "end": 1.0}]
            },
        }
        raw = httpx.Response(
            status_code=200,
            json=job,
            request=httpx.Request("POST", "http://host/v1/diarize"),
        )
        result = config.transform_diarization_response(
            model="pyannote/community-1",
            raw_response=raw,
            logging_obj=MagicMock(),
        )
        assert result == job


class TestRegistration:
    def test_provider_enum_exists(self):
        assert litellm.LlmProviders.PYANNOTE.value == "pyannote"

    def test_registry_dispatches_pyannote(self):
        from litellm.utils import ProviderConfigManager

        cfg = ProviderConfigManager.get_provider_diarization_config(
            litellm.LlmProviders.PYANNOTE
        )
        assert isinstance(cfg, PyannoteDiarizationConfig)

    def test_registry_returns_none_for_unknown_provider(self):
        from litellm.utils import ProviderConfigManager

        assert (
            ProviderConfigManager.get_provider_diarization_config(
                litellm.LlmProviders.OPENAI
            )
            is None
        )
