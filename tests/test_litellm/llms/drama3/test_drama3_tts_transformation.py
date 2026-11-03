import pytest

import litellm
from litellm.llms.drama3.text_to_speech.transformation import (
    Drama3TextToSpeechConfig,
)


def test_drama3_in_provider_list():
    assert "drama3" in litellm.provider_list


def test_drama3_get_provider_text_to_speech_config():
    from litellm.types.utils import LlmProviders
    from litellm.utils import ProviderConfigManager

    config = ProviderConfigManager.get_provider_text_to_speech_config(
        model="drama3/fishaudio/drama-3",
        provider=LlmProviders.DRAMA3,
    )
    assert isinstance(config, Drama3TextToSpeechConfig)


def test_drama3_get_provider_voice_config():
    from litellm.types.utils import LlmProviders
    from litellm.utils import ProviderConfigManager

    config = ProviderConfigManager.get_provider_voice_config(
        provider=LlmProviders.DRAMA3,
    )
    assert config is not None
    assert config.__class__.__name__ == "Drama3VoiceConfig"


def test_drama3_tts_url():
    config = Drama3TextToSpeechConfig()
    url = config.get_complete_url(
        model="fishaudio/drama-3",
        api_base="http://10.43.173.29:8080",
        litellm_params={},
    )
    assert url == "http://10.43.173.29:8080/v1/audio/speech"


def test_drama3_tts_url_strips_trailing_v1():
    config = Drama3TextToSpeechConfig()
    url = config.get_complete_url(
        model="fishaudio/drama-3",
        api_base="http://10.43.173.29:8080/v1",
        litellm_params={},
    )
    assert url == "http://10.43.173.29:8080/v1/audio/speech"


def test_drama3_tts_url_requires_api_base():
    config = Drama3TextToSpeechConfig()
    with pytest.raises(Exception, match="Missing Drama 3 API base"):
        config.get_complete_url(
            model="fishaudio/drama-3", api_base=None, litellm_params={}
        )


REFERENCES = [
    {
        "audio_path": "https://example.com/ref.wav",
        "text": "We asked over twenty different people.",
    }
]


def test_drama3_tts_maps_references_and_sampling_params():
    """The exact fields the pod's schema accepts must survive map_openai_params.

    These are the fields the OpenAI TTS branch drops today (references,
    top_k, top_p, temperature, max_new_tokens, stream), which is the whole
    reason this provider exists.
    """
    config = Drama3TextToSpeechConfig()
    optional_params = {
        "response_format": "wav",
        "speed": 1.0,
        "references": REFERENCES,
        "top_k": 30,
        "top_p": 0.9,
        "temperature": 1.0,
        "max_new_tokens": 500,
        "stream": True,
        "stream_format": "sse",
    }
    voice, mapped = config.map_openai_params(
        model="fishaudio/drama-3",
        optional_params=optional_params,
        voice="default",
        drop_params=False,
        kwargs={},
    )
    assert voice == "default"
    assert mapped["references"] == REFERENCES
    assert mapped["top_k"] == 30
    assert mapped["top_p"] == 0.9
    assert mapped["temperature"] == 1.0
    assert mapped["max_new_tokens"] == 500
    assert mapped["stream"] is True
    assert mapped["stream_format"] == "sse"
    assert mapped["response_format"] == "wav"

    request_data = config.transform_text_to_speech_request(
        model="fishaudio/drama-3",
        input="Get the trust fund to the bank early.",
        voice="default",
        optional_params=dict(mapped),
        litellm_params={},
        headers={},
    )
    body = request_data["dict_body"]
    assert body["input"] == "Get the trust fund to the bank early."
    assert body["voice"] == "default"
    assert body["references"] == REFERENCES
    assert body["top_k"] == 30
    assert body["stream"] is True
    assert body["stream_format"] == "sse"


def test_drama3_tts_instructions_forwards_to_pod_validator():
    """instructions is a schema field of the pod's CreateSpeechRequest and the
    old hosted_vllm path forwarded it (it is one of speech()'s three whitelisted
    params), so drama-3's own per-model 400 is the honest answer. The first cut
    of this config silently dropped it, turning that 400 into a 200 with the
    style request ignored, a regression against the pre-provider behavior."""
    config = Drama3TextToSpeechConfig()
    voice, mapped = config.map_openai_params(
        model="fishaudio/drama-3",
        optional_params={"instructions": "speak slowly"},
        voice="default",
        drop_params=False,
        kwargs={},
    )
    assert mapped["instructions"] == "speak slowly"


def test_drama3_tts_extra_body_forwarded():
    config = Drama3TextToSpeechConfig()
    voice, mapped = config.map_openai_params(
        model="fishaudio/drama-3",
        optional_params={"extra_body": {"seed": 42}},
        voice="default",
        drop_params=False,
        kwargs={},
    )
    assert mapped["seed"] == 42


def test_drama3_tts_default_voice_when_none():
    config = Drama3TextToSpeechConfig()
    voice, mapped = config.map_openai_params(
        model="fishaudio/drama-3",
        optional_params={},
        voice=None,
        drop_params=False,
        kwargs={},
    )
    assert voice is None

    request_data = config.transform_text_to_speech_request(
        model="fishaudio/drama-3",
        input="hello",
        voice=None,
        optional_params={},
        litellm_params={},
        headers={},
    )
    body = request_data["dict_body"]
    assert body["voice"] == "default"


def test_drama3_tts_internal_params_never_forwarded():
    """Proxy-internal kwargs must not leak into the pod body."""
    config = Drama3TextToSpeechConfig()
    voice, mapped = config.map_openai_params(
        model="fishaudio/drama-3",
        optional_params={},
        voice="default",
        drop_params=False,
        kwargs={
            "litellm_call_id": "abc",
            "proxy_server_request": {"path": "/v1/audio/speech"},
            "metadata": {"user": "x"},
            "model_info": {"mode": "audio_speech"},
            "top_k": 20,
        },
    )
    assert "litellm_call_id" not in mapped
    assert "proxy_server_request" not in mapped
    assert "metadata" not in mapped
    assert "model_info" not in mapped
    assert mapped["top_k"] == 20


def test_drama3_tts_kwargs_path_forwards_stream_and_references():
    """The proxy route passes stream/references as **kwargs into speech(), not
    as named optional_params. The kwargs loop must forward them or the gateway
    drops them exactly like the OpenAI branch does."""
    config = Drama3TextToSpeechConfig()
    voice, mapped = config.map_openai_params(
        model="fishaudio/drama-3",
        optional_params={"response_format": "wav"},
        voice="default",
        drop_params=False,
        kwargs={
            "stream": True,
            "stream_format": "sse",
            "references": REFERENCES,
        },
    )
    assert mapped["stream"] is True
    assert mapped["stream_format"] == "sse"
    assert mapped["references"] == REFERENCES


def test_drama3_tts_kwargs_path_forwards_pod_schema_fields():
    """language, temperature, seed, and task_type are top-level fields of the
    pod's CreateSpeechRequest, but the proxy delivers them via **kwargs. The
    first cut of this provider copied HAMSA_INTERNAL_PARAMS wholesale, which
    lists language and temperature as internal chat params, and the kwargs
    loop silently dropped them again: the exact defect the provider exists to
    fix, alive in two fields. Verified on the wire before the fix:
    aspeech(..., temperature=0.8, language="en") sent neither.
    """
    config = Drama3TextToSpeechConfig()
    voice, mapped = config.map_openai_params(
        model="fishaudio/drama-3",
        optional_params={},
        voice="default",
        drop_params=False,
        kwargs={
            "temperature": 0.8,
            "language": "en",
            "seed": 42,
            "task_type": "Base",
        },
    )
    assert mapped["temperature"] == 0.8
    assert mapped["language"] == "en"
    assert mapped["seed"] == 42
    assert mapped["task_type"] == "Base"


def test_drama3_tts_deployment_litellm_params_never_leak_to_pod():
    """The router merges the deployment's ENTIRE litellm_params dict into the
    speech() kwargs (router.py _aspeech: {**data, **kwargs}), so any key on the
    model row rides into map_openai_params. The hand-copied internal frozenset
    missed allow_client_keepalive_override and keepalive_seconds, and the
    deployed pod 400'd every request with "unknown speech request field" the
    moment the provider went live. The canonical is_litellm_owned_kwarg
    inventory filters the whole litellm-owned family, present and future.

    These are the REAL keys from the live dev row's litellm_params, plus the
    generic owned set a deployment can carry.
    """
    config = Drama3TextToSpeechConfig()
    kwargs = {
        "allow_client_keepalive_override": False,
        "keepalive_seconds": 20.0,
        "use_in_pass_through": True,
        "use_litellm_proxy": True,
        "use_xai_oauth": False,
        "merge_reasoning_content_in_choices": False,
        "stream": True,
        "references": [{"audio_path": "https://x/r.wav", "text": "t"}],
        "top_k": 30,
    }
    voice, mapped = config.map_openai_params(
        model="fishaudio/drama-3",
        optional_params={},
        voice="default",
        drop_params=False,
        kwargs=kwargs,
    )
    for leaked in (
        "allow_client_keepalive_override",
        "keepalive_seconds",
        "use_in_pass_through",
        "use_litellm_proxy",
        "use_xai_oauth",
        "merge_reasoning_content_in_choices",
    ):
        assert leaked not in mapped, f"{leaked} leaked to the pod body"
    assert mapped["stream"] is True
    assert mapped["references"]
    assert mapped["top_k"] == 30

    request_data = config.transform_text_to_speech_request(
        model="fishaudio/drama-3",
        input="probe",
        voice="default",
        optional_params=dict(mapped),
        litellm_params={},
        headers={},
    )
    body = request_data["dict_body"]
    assert "allow_client_keepalive_override" not in body
    assert "keepalive_seconds" not in body
    assert body["stream"] is True
