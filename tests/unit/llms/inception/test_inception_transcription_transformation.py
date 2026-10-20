import httpx

from litellm.llms.inception.transcription.transformation import (
    InceptionAudioTranscriptionConfig,
)


def test_inception_stt_supported_params():
    params = InceptionAudioTranscriptionConfig().get_supported_openai_params("inception-stt")
    for p in ("language", "prompt", "response_format", "temperature"):
        assert p in params, f"{p} should be a supported STT param"


def test_inception_stt_url():
    config = InceptionAudioTranscriptionConfig()
    url = config.get_complete_url(
        api_base="http://10.0.0.5:8000",
        api_key=None,
        model="inception-stt",
        optional_params={},
        litellm_params={},
    )
    assert url == "http://10.0.0.5:8000/v1/audio/transcriptions"


def test_inception_stt_url_strips_trailing_slash():
    config = InceptionAudioTranscriptionConfig()
    url = config.get_complete_url(
        api_base="http://10.0.0.5:8000/",
        api_key=None,
        model="inception-stt",
        optional_params={},
        litellm_params={},
    )
    assert url == "http://10.0.0.5:8000/v1/audio/transcriptions"


def test_inception_stt_url_strips_double_v1():
    config = InceptionAudioTranscriptionConfig()
    url = config.get_complete_url(
        api_base="http://10.0.0.5:8000/v1",
        api_key=None,
        model="inception-stt",
        optional_params={},
        litellm_params={},
    )
    assert url == "http://10.0.0.5:8000/v1/audio/transcriptions"


def test_inception_stt_url_requires_api_base():
    config = InceptionAudioTranscriptionConfig()
    try:
        config.get_complete_url(
            api_base=None,
            api_key=None,
            model="inception-stt",
            optional_params={},
            litellm_params={},
        )
        assert False, "Should have raised for missing api_base"
    except Exception as e:
        assert "Missing Inception API base" in str(e)


def test_inception_stt_request_multipart_form():
    config = InceptionAudioTranscriptionConfig()
    optional_params = config.map_openai_params(
        non_default_params={"language": "ar", "response_format": "verbose_json"},
        optional_params={},
        model="inception-stt",
        drop_params=False,
    )
    assert optional_params["language"] == "ar"
    assert optional_params["response_format"] == "verbose_json"

    audio_bytes = b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00"
    request_data = config.transform_audio_transcription_request(
        model="inception-stt",
        audio_file=("test.wav", audio_bytes, "audio/wav"),
        optional_params=optional_params,
        litellm_params={},
    )
    assert request_data.files is not None
    assert "file" in request_data.files
    filename, content, content_type = request_data.files["file"]
    assert content == audio_bytes
    assert content_type == "audio/wav"
    assert request_data.data["model"] == "inception-stt"
    assert request_data.data["language"] == "ar"
    assert request_data.data["response_format"] == "verbose_json"


def test_inception_stt_response_preserves_audio_duration_and_word_timestamps():
    config = InceptionAudioTranscriptionConfig()
    word_timestamps = [
        {"word": "مرحبا", "start": 0.0, "end": 0.5},
        {"word": "بكم", "start": 0.5, "end": 1.0},
    ]
    payload = {
        "text": "مرحبا بكم",
        "audio_duration": 1.5,
        "word_timestamps": word_timestamps,
    }
    fake_response = httpx.Response(
        status_code=200,
        json=payload,
        request=httpx.Request("POST", "http://x/v1/audio/transcriptions"),
    )
    result = config.transform_audio_transcription_response(fake_response)
    assert result.text == "مرحبا بكم"
    assert result["audio_duration"] == 1.5
    assert result["word_timestamps"] == word_timestamps
    assert result._hidden_params["audio_duration"] == 1.5
    assert result._hidden_params["word_timestamps"] == word_timestamps


def test_inception_stt_response_minimal_text_only():
    config = InceptionAudioTranscriptionConfig()
    payload = {"text": "hello world"}
    fake_response = httpx.Response(
        status_code=200,
        json=payload,
        request=httpx.Request("POST", "http://x/v1/audio/transcriptions"),
    )
    result = config.transform_audio_transcription_response(fake_response)
    assert result.text == "hello world"
    assert result["task"] == "transcribe"


def test_inception_stt_response_includes_duration_and_language_for_verbose():
    config = InceptionAudioTranscriptionConfig()
    payload = {
        "text": "مرحبا",
        "audio_duration": 0.8,
        "duration": 0.8,
        "language": "arabic",
    }
    fake_response = httpx.Response(
        status_code=200,
        json=payload,
        request=httpx.Request("POST", "http://x/v1/audio/transcriptions"),
    )
    result = config.transform_audio_transcription_response(fake_response)
    assert result.text == "مرحبا"
    assert result["audio_duration"] == 0.8
    assert result["duration"] == 0.8
    assert result["language"] == "arabic"


def test_inception_stt_validate_environment_does_not_force_content_type():
    config = InceptionAudioTranscriptionConfig()
    headers = config.validate_environment(
        headers={},
        model="inception-stt",
        messages=[],
        optional_params={},
        litellm_params={},
        api_key=None,
        api_base="http://x",
    )
    assert "Content-Type" not in headers


def test_inception_stt_internal_params_filtered_from_form():
    config = InceptionAudioTranscriptionConfig()
    optional_params = config.map_openai_params(
        non_default_params={
            "language": "ar",
            "api_key": "secret",
            "litellm_call_id": "abc",
        },
        optional_params={},
        model="inception-stt",
        drop_params=False,
    )
    assert "api_key" not in optional_params
    assert "litellm_call_id" not in optional_params
    assert optional_params["language"] == "ar"
