"""Behavior tests for the audio translation SDK slice (litellm/endpoints/audio_translation).

Pins the public entrypoint contract: ``litellm.atranslation``/``litellm.translation``
resolve to the slice, the OpenAI provider dispatches through the official SDK
transport (``audio.translations.with_raw_response``), native self-hosted
providers dispatch to the generic http handler with their own config, the
file checksum is computed before any API call and is namespaced so a
translation cache entry cannot collide with a transcription of the same
file, and a provider with no translation config fails with a clear 400
rather than silently borrowing a transcription path.
"""

from __future__ import annotations

import asyncio
import io
from typing import Any, Final
from unittest.mock import AsyncMock, MagicMock

import pytest

import litellm
from litellm.types.llms.audio_transcription import NativeAudioDocumentResponse
from litellm.types.utils import TranscriptionResponse

_AUDIO: Final = b"fake audio bytes for translation"


def _audio_file() -> io.BytesIO:
    audio = io.BytesIO(_AUDIO)
    audio.name = "meeting.wav"
    return audio


def _logging_obj() -> MagicMock:
    logging_obj = MagicMock()
    logging_obj.pre_call = MagicMock()
    logging_obj.post_call = MagicMock()
    logging_obj.update_environment_variables = MagicMock()
    logging_obj.model_call_details = {}
    return logging_obj


class TestOpenAIBranchUsesOfficialSdkTransport:
    def _run_translation(self, data_extra: dict[str, Any]) -> dict[str, Any]:
        from litellm.llms.openai.translation.handler import OpenAIAudioTranslation

        captured: dict[str, Any] = {}

        class _RawResponse:
            def __init__(self) -> None:
                self.headers = {"content-type": "application/json"}

            def parse(self) -> TranscriptionResponse:
                parsed = TranscriptionResponse(text="hello translated")
                parsed.duration = 1.5
                return parsed

        async def _fake_create(**kwargs: Any) -> _RawResponse:
            captured.update(kwargs)
            return _RawResponse()

        aclient = MagicMock()
        aclient.api_key = "sk-test"
        aclient._base_url = MagicMock()
        aclient._base_url._uri_reference = "https://api.openai.com/v1"
        aclient.audio.translations.with_raw_response.create = _fake_create

        result = asyncio.run(
            OpenAIAudioTranslation().async_audio_translations(
                audio_file=_audio_file(),
                data={"model": "whisper-1", "file": _audio_file(), **data_extra},
                model_response=litellm.utils.TranscriptionResponse(),
                timeout=30,
                logging_obj=_logging_obj(),
                api_key="sk-test",
                api_base=None,
                client=aclient,
                max_retries=0,
            )
        )
        assert isinstance(result, TranscriptionResponse)
        assert result.text == "hello translated"
        return captured

    def test_calls_the_official_translations_endpoint(self) -> None:
        captured = self._run_translation({})
        assert "model" in captured
        assert "timeout" in captured

    def test_extra_body_is_passed_to_the_sdk_unchanged(self) -> None:
        captured = self._run_translation({"extra_body": {"verbosity": "low"}})
        assert captured.get("extra_body") == {"verbosity": "low"}

    @pytest.mark.asyncio
    async def test_sdk_string_document_keeps_its_body(self) -> None:
        from litellm.llms.openai.translation.handler import OpenAIAudioTranslation

        class _RawResponse:
            headers = {"content-type": "text/plain; charset=iso-8859-1"}

            def parse(self) -> str:
                return "déjà vu"

        aclient = MagicMock()
        aclient.api_key = "sk-test"
        aclient._base_url._uri_reference = "https://api.openai.com/v1"
        aclient.audio.translations.with_raw_response.create = AsyncMock(return_value=_RawResponse())
        result = await OpenAIAudioTranslation().async_audio_translations(
            audio_file=_audio_file(),
            data={"model": "whisper-1", "file": _audio_file(), "response_format": "text"},
            model_response=TranscriptionResponse(),
            timeout=30,
            logging_obj=_logging_obj(),
            api_key="sk-test",
            client=aclient,
        )
        assert isinstance(result, NativeAudioDocumentResponse)
        assert result.body == "déjà vu"
        assert result.charset == "iso-8859-1"

    def test_sync_sdk_string_document_keeps_its_body(self) -> None:
        from litellm.llms.openai.translation.handler import OpenAIAudioTranslation

        class _RawResponse:
            headers = {"content-type": "text/plain"}

            def parse(self) -> str:
                return "translated text"

        aclient = MagicMock()
        aclient.api_key = "sk-test"
        aclient._base_url._uri_reference = "https://api.openai.com/v1"
        aclient.audio.translations.with_raw_response.create = MagicMock(return_value=_RawResponse())
        result = OpenAIAudioTranslation().audio_translations(
            audio_file=_audio_file(),
            data={"model": "whisper-1", "file": _audio_file(), "response_format": "text"},
            model_response=TranscriptionResponse(),
            timeout=30,
            logging_obj=_logging_obj(),
            api_key="sk-test",
            client=aclient,
        )
        assert isinstance(result, NativeAudioDocumentResponse)
        assert result.body == "translated text"

    def test_cached_rich_response_hydration_does_not_use_text_only_constructor(self) -> None:
        from litellm.litellm_core_utils.llm_response_utils.transcriptions import hydrate_transcription_response

        result = hydrate_transcription_response(
            {"text": "cached", "usage": None, "duration": 1.5, "native_field": "preserved"}
        )
        assert result.text == "cached"
        assert result["duration"] == 1.5
        assert result["native_field"] == "preserved"


class TestNativeBranchUsesProviderConfig:
    def test_native_provider_hits_generic_http_handler_with_nim_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import litellm.endpoints.audio_translation.main as translation_slice
        from litellm.llms.nvidia_nim.audio_translation.transformation import (
            NvidiaNimAudioTranslationConfig,
        )

        captured: dict[str, Any] = {}

        class _InjectedHandler:
            def audio_translations(self, **kwargs: Any):
                captured.update(kwargs)
                return TranscriptionResponse(text="hello translated")

        monkeypatch.setattr(translation_slice, "BaseLLMHTTPHandler", lambda: _InjectedHandler())

        result = asyncio.run(
            litellm.atranslation(
                model="nvidia_nim/nvidia/parakeet-mlx-v3",
                file=_audio_file(),
                target_language="en",
            )
        )
        assert result.text == "hello translated"
        assert isinstance(captured["provider_config"], NvidiaNimAudioTranslationConfig)
        assert captured["optional_params"]["target_language"] == "en"

    def test_unsupported_provider_fails_with_clear_400_not_silent_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import litellm.endpoints.audio_translation.main as translation_slice

        class _InjectedHandler:
            def audio_translations(self, **kwargs: Any) -> None:
                raise AssertionError("generic http handler must not be reached for a provider without a config")

        monkeypatch.setattr(translation_slice, "BaseLLMHTTPHandler", lambda: _InjectedHandler())

        with pytest.raises(litellm.BadRequestError, match="not supported"):
            asyncio.run(litellm.atranslation(model="groq/whisper-large-v3", file=_audio_file()))


class TestCacheIdentity:
    def test_optional_params_never_leak_the_constants_binding_into_the_request(self) -> None:
        """The builder's local ``OPENAI_TRANSLATION_PARAMS`` import must not ride
        into the request: on the native transport it would become a multipart
        form field named ``OPENAI_TRANSLATION_PARAMS`` and reach the server."""
        from litellm.utils import get_optional_params_translation

        params = get_optional_params_translation(
            model="m", custom_llm_provider="nvidia_nim", target_language="en", prompt="ctx"
        )
        assert "OPENAI_TRANSLATION_PARAMS" not in params
        assert params == {"prompt": "ctx", "target_language": "en"}

    def test_translation_checksum_is_namespaced_away_from_transcription(self) -> None:
        """The file identity that keys the response cache carries the operation.

        The cache keys on metadata file_checksum + model with no operation
        component, so a bare checksum would make a translation return a
        cached transcription of the same file (and vice versa). The
        "translation:" namespace keeps the two entries disjoint while
        identical translation requests still collide with each other, which
        is the desired cache hit.
        """
        from litellm.caching.caching import Cache
        from litellm.litellm_core_utils.audio_utils.utils import (
            get_audio_file_content_hash,
        )

        cache = Cache(type="local")
        checksum: Final = get_audio_file_content_hash(file_obj=_audio_file())

        transcription_key = cache.get_cache_key(
            model="m", file="x", litellm_params={}, metadata={"file_checksum": checksum}
        )
        translation_key = cache.get_cache_key(
            model="m", file="x", litellm_params={}, metadata={"file_checksum": f"translation:{checksum}"}
        )
        translation_key_repeat = cache.get_cache_key(
            model="m", file="x", litellm_params={}, metadata={"file_checksum": f"translation:{checksum}"}
        )

        assert transcription_key != translation_key
        assert translation_key == translation_key_repeat

    @pytest.mark.asyncio
    @pytest.mark.parametrize("sync", [False, True])
    async def test_native_options_use_independent_public_cache_entries(
        self, monkeypatch: pytest.MonkeyPatch, sync: bool
    ) -> None:
        from litellm.caching.caching import Cache
        from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler, HTTPHandler

        calls: list[str] = []

        def responder(request: Any) -> Any:
            import httpx

            content = request.read().decode()
            target = "fr" if 'name="target_language"\r\n\r\nfr' in content else "en"
            calls.append(target)
            return httpx.Response(200, json={"text": f"translated-{target}", "usage": None})

        async def translate(target: str) -> TranscriptionResponse:
            import httpx

            if sync:
                return litellm.translation(
                    model="nvidia_nim/m",
                    file=_audio_file(),
                    target_language=target,
                    api_base="http://nim/v1",
                    client=HTTPHandler(client=httpx.Client(transport=httpx.MockTransport(responder))),
                )
            return await litellm.atranslation(
                model="nvidia_nim/m",
                file=_audio_file(),
                target_language=target,
                api_base="http://nim/v1",
                client=AsyncHTTPHandler(transport=httpx.MockTransport(responder)),
            )

        original_cache: Final = litellm.cache
        litellm.cache = Cache(type="local")
        litellm.disable_cache = False
        monkeypatch.setattr(litellm, "cache", litellm.cache)
        try:
            english = await translate("en")
            french = await translate("fr")
            repeated = await translate("en")
        finally:
            litellm.cache = original_cache
        assert isinstance(english, TranscriptionResponse)
        assert english.text == "translated-en"
        assert french.text == "translated-fr"
        assert repeated.text == english.text
        assert repeated.hidden_params.get("cache_hit") is True
        assert calls == ["en", "fr"]

    def test_function_setup_stamps_the_namespaced_checksum(self) -> None:
        """function_setup must write the namespaced identity, not the bare checksum."""
        import datetime as dt

        audio = _audio_file()
        from litellm.litellm_core_utils.audio_utils.utils import get_audio_file_content_hash

        bare: Final = get_audio_file_content_hash(file_obj=audio)

        logging_obj, kwargs = litellm.utils.function_setup(
            "atranslation",
            litellm.Rules(),
            dt.datetime.now(),
            "m",
            audio,
            litellm_call_id="test-call-id",
        )
        stamped: Final = kwargs["metadata"]["file_checksum"]
        assert stamped == f"translation:{bare}"
        assert not stamped.startswith("translation:translation:")
