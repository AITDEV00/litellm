import httpx
import pytest

import litellm
from litellm.llms.custom_httpx.llm_http_handler import AsyncHTTPHandler
from litellm.proxy.voice_routes import _audio_media_type


class _Client(AsyncHTTPHandler):
    def __init__(self) -> None:
        super().__init__()
        self.client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    content=b"\x00\x01" * 16,
                    headers={"content-type": "audio/wav"},
                    request=request,
                )
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("omnivoice/omni", "hamsa/hamsa-stt"))
@pytest.mark.parametrize(("response_format", "media_type"), (("wav", "audio/wav"), ("pcm", "audio/pcm")))
async def test_native_speech_keeps_requested_format_for_proxy_headers(
    provider: str,
    response_format: str,
    media_type: str,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("HAMSA_API_KEY", "synthetic-upstream-key")
    voice = "alloy" if provider.startswith("omnivoice") else "Zeina"
    response = await litellm.aspeech(
        model=provider,
        input="hello",
        voice=voice,
        response_format=response_format,
        stream=response_format == "pcm",
        api_base=f"http://{provider.split('/', 1)[0]}.test/v1",
        client=_Client(),
    )

    assert response.hidden_params["response_format"] == response_format
    assert _audio_media_type({"response_format": response_format}, response) == media_type


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response_format", "stream", "expected_format", "expected_media_type"),
    (
        ("mulaw", False, "wav", "audio/wav"),
        ("mulaw", True, "mulaw", "audio/basic"),
        ("pcm", False, "wav", "audio/wav"),
        (None, True, "pcm", "audio/pcm"),
    ),
)
async def test_hamsa_speech_preserves_wav_container_semantics(
    response_format: str | None,
    stream: bool,
    expected_format: str | None,
    expected_media_type: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HAMSA_API_KEY", "synthetic-upstream-key")
    response = await litellm.aspeech(
        model="hamsa/hamsa-stt",
        input="hello",
        voice="Zeina",
        response_format=response_format,
        stream=stream,
        api_base="http://hamsa.test/v1",
        client=_Client(),
    )

    assert response.hidden_params["response_format"] == expected_format
    assert _audio_media_type({"response_format": response_format}, response) == expected_media_type


@pytest.mark.asyncio
async def test_hamsa_clone_does_not_promise_an_unforwarded_pcm_format(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HAMSA_API_KEY", "synthetic-upstream-key")
    response = await litellm.aspeech(
        model="hamsa/hamsa-stt",
        input="hello",
        voice="clone",
        response_format="pcm",
        ref_audio=("reference.wav", b"RIFF0000", "audio/wav"),
        api_base="http://hamsa.test/v1",
        client=_Client(),
    )

    assert response.content_type == "audio/wav"
    assert response.hidden_params.get("response_format") is None
    assert _audio_media_type({"response_format": "pcm"}, response) == "audio/wav"
