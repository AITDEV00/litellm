import httpx
from unittest import mock

from litellm.llms.omnivoice.voice.transformation import OmniVoiceVoiceCloneConfig


def test_omnivoice_voice_clone_url():
    config = OmniVoiceVoiceCloneConfig()
    url = config.get_complete_url(
        model="omnivoice",
        api_base="http://10.43.173.29:8080",
        litellm_params={},
    )
    assert url == "http://10.43.173.29:8080/v1/audio/speech/clone"


def test_omnivoice_voice_clone_url_strips_trailing_v1():
    config = OmniVoiceVoiceCloneConfig()
    url = config.get_complete_url(
        model="omnivoice",
        api_base="http://10.43.173.29:8080/v1",
        litellm_params={},
    )
    assert url == "http://10.43.173.29:8080/v1/audio/speech/clone"


def test_omnivoice_voice_clone_url_requires_api_base():
    config = OmniVoiceVoiceCloneConfig()
    try:
        config.get_complete_url(model="omnivoice", api_base=None, litellm_params={})
        assert False, "Should have raised for missing api_base"
    except Exception as e:
        assert "Missing OmniVoice API base" in str(e)


def test_omnivoice_voice_clone_request_builds_multipart_form_data():
    config = OmniVoiceVoiceCloneConfig()
    voice, mapped = config.map_openai_params(
        model="omnivoice",
        optional_params={
            "response_format": "mp3",
            "speed": 1.0,
            "language": "ar",
            "num_step": 32,
            "guidance_scale": 3.0,
            "denoise": True,
        },
        voice="clone",
        drop_params=False,
        kwargs={
            "ref_audio": ("ref.wav", b"\x52\x49\x46\x46", "audio/wav"),
            "ref_text": "reference transcript",
        },
    )

    request_data = config.transform_text_to_speech_request(
        model="omnivoice",
        input="hello world",
        voice="clone",
        optional_params=mapped,
        litellm_params={},
        headers={},
    )

    assert "form_data" in request_data
    assert "files" in request_data
    assert "dict_body" not in request_data

    form = request_data["form_data"]
    assert form["text"] == "hello world"
    assert form["ref_text"] == "reference transcript"
    assert form["response_format"] == "mp3"
    assert form["speed"] == 1.0
    assert form["language"] == "ar"
    assert form["num_step"] == 32
    assert form["guidance_scale"] == 3.0
    assert form["denoise"] is True

    files = request_data["files"]
    assert "ref_audio" in files
    assert files["ref_audio"][0] == "ref.wav"
    assert files["ref_audio"][1] == b"\x52\x49\x46\x46"
    assert files["ref_audio"][2] == "audio/wav"


def test_omnivoice_voice_clone_request_without_ref_text():
    config = OmniVoiceVoiceCloneConfig()
    voice, mapped = config.map_openai_params(
        model="omnivoice",
        optional_params={},
        voice="clone",
        drop_params=False,
        kwargs={
            "ref_audio": ("ref.wav", b"\x52\x49\x46\x46", "audio/wav"),
        },
    )

    request_data = config.transform_text_to_speech_request(
        model="omnivoice",
        input="hello",
        voice="clone",
        optional_params=mapped,
        litellm_params={},
        headers={},
    )

    assert "ref_text" not in request_data["form_data"]
    assert request_data["form_data"]["text"] == "hello"


def test_omnivoice_voice_clone_request_with_bytes_ref_audio():
    config = OmniVoiceVoiceCloneConfig()
    voice, mapped = config.map_openai_params(
        model="omnivoice",
        optional_params={},
        voice="clone",
        drop_params=False,
        kwargs={
            "ref_audio": b"\x52\x49\x46\x46\x00\x00\x00\x00",
        },
    )

    request_data = config.transform_text_to_speech_request(
        model="omnivoice",
        input="hello",
        voice="clone",
        optional_params=mapped,
        litellm_params={},
        headers={},
    )

    files = request_data["files"]
    assert "ref_audio" in files
    assert files["ref_audio"][0] == "ref_audio.wav"
    assert files["ref_audio"][1] == b"\x52\x49\x46\x46\x00\x00\x00\x00"
    assert files["ref_audio"][2] == "audio/wav"


def test_omnivoice_voice_clone_request_without_ref_audio_raises():
    config = OmniVoiceVoiceCloneConfig()
    voice, mapped = config.map_openai_params(
        model="omnivoice",
        optional_params={},
        voice="clone",
        drop_params=False,
        kwargs={},
    )

    try:
        config.transform_text_to_speech_request(
            model="omnivoice",
            input="hello",
            voice="clone",
            optional_params=mapped,
            litellm_params={},
            headers={},
        )
        assert False, "Should have raised for missing ref_audio"
    except Exception as e:
        assert "ref_audio" in str(e)


def test_omnivoice_voice_clone_instructions_filtered():
    config = OmniVoiceVoiceCloneConfig()
    voice, mapped = config.map_openai_params(
        model="omnivoice",
        optional_params={"instructions": "speak slowly"},
        voice="clone",
        drop_params=False,
        kwargs={
            "ref_audio": ("ref.wav", b"\x00", "audio/wav"),
        },
    )
    assert "instructions" not in mapped


def test_omnivoice_voice_clone_internal_params_filtered():
    config = OmniVoiceVoiceCloneConfig()
    voice, mapped = config.map_openai_params(
        model="omnivoice",
        optional_params={},
        voice="clone",
        drop_params=False,
        kwargs={
            "ref_audio": ("ref.wav", b"\x00", "audio/wav"),
            "api_key": "secret",
            "litellm_call_id": "abc",
            "proxy_server_request": {},
            "custom_param": "kept",
        },
    )
    assert "api_key" not in mapped
    assert "litellm_call_id" not in mapped
    assert "proxy_server_request" not in mapped
    assert mapped["ref_audio"] == ("ref.wav", b"\x00", "audio/wav")
    assert mapped["custom_param"] == "kept"


def test_omnivoice_voice_clone_secret_fields_filtered():
    config = OmniVoiceVoiceCloneConfig()
    voice, mapped = config.map_openai_params(
        model="omnivoice",
        optional_params={},
        voice="clone",
        drop_params=False,
        kwargs={
            "ref_audio": ("ref.wav", b"\x00", "audio/wav"),
            "secret_fields": {"raw_headers": {"authorization": "Bearer secret"}},
            "custom_param": "kept",
        },
    )
    assert "secret_fields" not in mapped
    assert mapped["custom_param"] == "kept"


def test_omnivoice_voice_clone_non_primitive_values_filtered():
    config = OmniVoiceVoiceCloneConfig()
    voice, mapped = config.map_openai_params(
        model="omnivoice",
        optional_params={},
        voice="clone",
        drop_params=False,
        kwargs={
            "ref_audio": ("ref.wav", b"\x00", "audio/wav"),
            "not_a_known_internal_dict": {"suspicious": "value"},
            "not_a_known_internal_list": [1, 2, 3],
            "custom_string_param": "kept",
            "custom_int_param": 42,
            "custom_float_param": 1.5,
            "custom_bool_param": True,
        },
    )
    assert "not_a_known_internal_dict" not in mapped
    assert "not_a_known_internal_list" not in mapped
    assert mapped["custom_string_param"] == "kept"
    assert mapped["custom_int_param"] == 42
    assert mapped["custom_float_param"] == 1.5
    assert mapped["custom_bool_param"] is True
    assert "ref_audio" in mapped


def test_omnivoice_voice_clone_router_level_params_filtered():
    config = OmniVoiceVoiceCloneConfig()
    voice, mapped = config.map_openai_params(
        model="omnivoice",
        optional_params={},
        voice="clone",
        drop_params=False,
        kwargs={
            "ref_audio": ("ref.wav", b"\x00", "audio/wav"),
            "use_in_pass_through": True,
            "use_litellm_proxy": True,
            "use_xai_oauth": True,
            "custom_param": "kept",
        },
    )
    assert "use_in_pass_through" not in mapped
    assert "use_litellm_proxy" not in mapped
    assert "use_xai_oauth" not in mapped
    assert mapped["custom_param"] == "kept"


def test_omnivoice_voice_clone_all_custom_params_forwarded():
    config = OmniVoiceVoiceCloneConfig()
    optional_params = {
        "response_format": "wav",
        "speed": 2.0,
        "stream": False,
        "num_step": 16,
        "guidance_scale": 5.0,
        "denoise": True,
        "t_shift": 1.0,
        "position_temperature": 3.0,
        "class_temperature": 1.0,
        "duration": 5.0,
        "language": "en",
        "layer_penalty_factor": 0.5,
        "preprocess_prompt": True,
        "postprocess_output": False,
        "audio_chunk_duration": 10.0,
        "audio_chunk_threshold": 0.5,
        "request_timeout_s": 30,
    }
    voice, mapped = config.map_openai_params(
        model="omnivoice",
        optional_params=optional_params,
        voice="clone",
        drop_params=False,
        kwargs={
            "ref_audio": ("ref.wav", b"\x00", "audio/wav"),
        },
    )
    for key, expected_value in optional_params.items():
        assert mapped.get(key) == expected_value, f"{key} should be forwarded"


def test_omnivoice_voice_clone_response_returns_binary():
    config = OmniVoiceVoiceCloneConfig()
    fake_response = httpx.Response(
        status_code=200,
        content=b"\x49\x44\x33\x03",
        headers={"content-type": "audio/wav"},
        request=httpx.Request("POST", "http://x/v1/audio/speech/clone"),
    )
    result = config.transform_text_to_speech_response(
        model="omnivoice",
        raw_response=fake_response,
        logging_obj=mock.Mock(),
    )
    assert result.response.headers["content-type"] == "audio/wav"


def test_omnivoice_voice_clone_validate_environment_does_not_force_content_type():
    config = OmniVoiceVoiceCloneConfig()
    headers = config.validate_environment(headers={}, model="omnivoice", api_key=None, api_base="http://x")
    assert "Content-Type" not in headers


def test_omnivoice_voice_clone_no_api_key_required():
    assert OmniVoiceVoiceCloneConfig.get_api_key() == "no-api-key-required"
