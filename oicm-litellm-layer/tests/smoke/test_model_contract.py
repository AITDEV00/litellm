"""Tier B: model contract checks. Costs a small amount of inference.

Every request needs the scoped KEY (export KEY=sk-...). Deterministic where the
feature's contract is deterministic; envelope/shape elsewhere. Never asserts a
provider's output text or a latency number.
"""

from __future__ import annotations

import base64
import io
import json
import os
import struct
import wave

import pytest

from .client import Gateway, Success, error_detail

pytestmark = pytest.mark.model_contract

GETTYSBURG = os.path.join(os.path.dirname(__file__), "..", "..", "..", "tests", "gettysburg.wav")


@pytest.fixture
def pcm_16khz() -> bytes:
    with wave.open(GETTYSBURG, "rb") as source:
        assert source.getframerate() == 22050
        assert source.getnchannels() == 1
        assert source.getsampwidth() == 2
        rate_ratio = source.getframerate() / 16000
        samples = struct.unpack(f"<{source.getnframes()}h", source.readframes(source.getnframes()))
    return struct.pack(
        f"<{int(len(samples) / rate_ratio)}h",
        *(samples[min(int(index * rate_ratio), len(samples) - 1)] for index in range(int(len(samples) / rate_ratio))),
    )


EMBED_MODELS = (
    ("Qwen/Qwen3-Embedding-4B", 2560),
    ("Qwen/Qwen3-Embedding-0.6B", 1024),
)


def _need_scoped(gw: Gateway) -> str:
    if not gw.scoped_key:
        pytest.fail("KEY not set: export a scoped llm_api key to run this check")
    return gw.scoped_key


class TestEmbeddings:
    @pytest.mark.parametrize("model, dims", EMBED_MODELS)
    def test_embedding_dimensions(self, gw: Gateway, model: str, dims: int) -> None:
        key = _need_scoped(gw)
        result = gw.post("/v1/embeddings", key=key, json={"model": model, "input": "hello world"})
        assert isinstance(result, Success), f"{model} failed: {error_detail(result)}"
        data = result.body.get("data")
        assert data and isinstance(data, list), f"{model} missing data"
        embedding = data[0].get("embedding")
        assert isinstance(embedding, list), f"{model} missing embedding"
        assert len(embedding) == dims, f"{model} embedding {len(embedding)} != {dims}"

    def test_batch_embeddings(self, gw: Gateway) -> None:
        key = _need_scoped(gw)
        result = gw.post(
            "/v1/embeddings",
            key=key,
            json={"model": "Qwen/Qwen3-Embedding-0.6B", "input": ["a", "b", "c"]},
        )
        assert isinstance(result, Success), f"batch embed failed: {error_detail(result)}"
        data = result.body.get("data")
        assert isinstance(data, list) and len(data) == 3, f"expected 3 rows, got {len(data or [])}"
        assert [d.get("index") for d in data] == [0, 1, 2], "batch indices not sequential"


class TestSpeechToText:
    def test_hamsa_stt_transcribes_fixture(self, gw: Gateway) -> None:
        key = _need_scoped(gw)
        if not os.path.exists(GETTYSBURG):
            pytest.fail(f"audio fixture missing: {GETTYSBURG}")
        with open(GETTYSBURG, "rb") as fh:
            result = gw.post(
                "/v1/audio/transcriptions",
                key=key,
                files={"file": ("audio.wav", fh, "audio/wav")},
                data={"model": "hamsa-stt", "language": "auto"},
            )
        assert isinstance(result, Success), f"hamsa-stt failed: {error_detail(result)}"
        assert result.body.get("text"), "transcription returned empty text"

    @pytest.mark.asyncio
    async def test_hamsa_stt_accepts_realtime_pcm(self, gw: Gateway, pcm_16khz: bytes) -> None:
        import asyncio
        import json

        import websockets

        key = _need_scoped(gw)
        url = gw.base.replace("http", "ws", 1) + "/v1/realtime?model=hamsa-stt"
        async with websockets.connect(
            url,
            additional_headers={"Authorization": f"Bearer {key}"},
            open_timeout=45,
            max_size=50 * 1024 * 1024,
        ) as socket:
            await socket.send(
                json.dumps(
                    {
                        "type": "handshake",
                        "options": {"silence_timeout": 30, "sample_rate": 16000, "audio_type": "PCM"},
                        "api_key": "",
                    }
                )
            )
            for _ in range(3):
                message = json.loads(await asyncio.wait_for(socket.recv(), 45))
                if message.get("type") == "handshake_ack":
                    break
            else:
                pytest.fail(f"Hamsa did not acknowledge the upstream handshake: {message}")
            for offset in range(0, len(pcm_16khz), 3200):
                await socket.send(pcm_16khz[offset : offset + 3200])
                if offset % 32000 == 0:
                    await asyncio.sleep(0.05)
            while True:
                message = json.loads(await asyncio.wait_for(socket.recv(), 90))
                if message.get("type") == "transcription":
                    data = message.get("data")
                    assert isinstance(data, dict) and data.get("transcription"), f"No realtime transcription: {message}"
                    break


class TestTextToSpeech:
    def test_tts_returns_audio(self, gw: Gateway) -> None:
        key = _need_scoped(gw)
        import httpx

        try:
            with httpx.Client(base_url=gw.base.rstrip("/"), timeout=120.0, verify=False) as c:
                resp = c.post(
                    "/v1/audio/speech",
                    headers={"Authorization": f"Bearer {key}"},
                    json={
                        "model": "hamsa-tts-new",
                        "input": "hello",
                        "voice": "Zeina",
                        "response_format": "wav",
                    },
                )
        except httpx.HTTPError as exc:
            pytest.fail(f"tts request failed: {exc}")
        assert resp.status_code == 200, f"tts {resp.status_code}: {resp.text[:200]}"
        assert resp.status_code == 200, f"tts {resp.status_code}: {resp.text[:200]}"
        assert resp.content[:4] == b"RIFF", "tts response is not WAV (RIFF header missing)"
        assert resp.content[8:12] == b"WAVE"
        assert len(resp.content) > 44, "tts WAV has no audio payload"
        with wave.open(io.BytesIO(resp.content)) as stream:
            assert stream.getframerate() > 0
            assert stream.getnchannels() > 0
            assert stream.getsampwidth() > 0
            assert stream.getnframes() > 0

    @pytest.mark.parametrize(("response_format", "media_type"), (("pcm", "audio/pcm"), ("mulaw", "audio/basic")))
    def test_hamsa_streaming_raw_codec_contract(self, gw: Gateway, response_format: str, media_type: str) -> None:
        import audioop

        import httpx

        with httpx.Client(
            base_url=gw.base, headers={"Authorization": f"Bearer {_need_scoped(gw)}"}, timeout=180, verify=False
        ) as client:
            with client.stream(
                "POST",
                "/v1/audio/speech",
                json={
                    "model": "hamsa-tts-new",
                    "input": "This checks the native streaming audio format.",
                    "voice": "Zeina",
                    "response_format": response_format,
                    "stream": True,
                },
            ) as response:
                assert response.status_code == 200, response.read()[:1000]
                assert response.headers.get("content-type", "").split(";")[0] == media_type
                content = b"".join(response.iter_raw())
        assert len(content) > 1000
        assert not content.startswith(b"RIFF")
        pcm = content if response_format == "pcm" else audioop.ulaw2lin(content, 2)
        assert len(pcm) % 2 == 0
        assert audioop.rms(pcm, 2) > 0

    @pytest.mark.parametrize("response_format", ("pcm", "mulaw"))
    def test_hamsa_buffered_audio_remains_wav(self, gw: Gateway, response_format: str) -> None:
        import httpx

        with httpx.Client(
            base_url=gw.base, headers={"Authorization": f"Bearer {_need_scoped(gw)}"}, timeout=180, verify=False
        ) as client:
            response = client.post(
                "/v1/audio/speech",
                json={
                    "model": "hamsa-tts-new",
                    "input": "This checks the buffered audio container.",
                    "voice": "adeo_mariam",
                    "response_format": response_format,
                    "stream": False,
                },
            )
        assert response.status_code == 200, response.text[:1000]
        assert response.headers.get("content-type", "").split(";")[0] == "audio/wav"
        assert response.content.startswith(b"RIFF") and response.content[8:12] == b"WAVE"
        with wave.open(io.BytesIO(response.content)) as stream:
            assert stream.getnframes() > 0
            assert stream.getnchannels() > 0
            assert stream.getsampwidth() > 0

    def test_drama_sse_events_and_content_type(self, gw: Gateway) -> None:
        import time

        import httpx

        pending = b""
        events = []
        with httpx.Client(
            base_url=gw.base, headers={"Authorization": f"Bearer {_need_scoped(gw)}"}, timeout=240, verify=False
        ) as client:
            with client.stream(
                "POST",
                "/v1/audio/speech",
                json={
                    "model": "fishaudio/drama-3",
                    "input": "This verifies the native event stream.",
                    "voice": "default",
                    "response_format": "pcm",
                    "stream": True,
                    "stream_format": "sse",
                },
            ) as response:
                assert response.status_code == 200, response.read()[:1000]
                assert response.headers.get("content-type", "").split(";")[0] == "text/event-stream"
                for chunk in response.iter_raw():
                    parts = (pending + chunk).replace(b"\r\n", b"\n").split(b"\n\n")
                    pending = parts.pop()
                    for frame in parts:
                        for line in frame.splitlines():
                            if line.startswith(b"data: "):
                                events.append((time.monotonic(), json.loads(line[6:])))
        assert not pending.strip()
        assert any(event.get("type") == "speech.audio.delta" and event.get("audio") for _, event in events)
        assert events[-1][1].get("type") == "speech.audio.done"
        delta_times = [arrival for arrival, event in events if event.get("type") == "speech.audio.delta"]
        assert len(delta_times) > 1
        assert delta_times[0] < events[-1][0]


class TestReasoningControl:
    def test_reasoning_effort_none_spends_zero_think_tokens(self, gw: Gateway) -> None:
        key = _need_scoped(gw)
        result = gw.post(
            "/v1/chat/completions",
            key=key,
            json={
                "model": "deepseek-ai/DeepSeek-V4.1-Flash",
                "messages": [{"role": "user", "content": "What is 2+2?"}],
                "reasoning_effort": "none",
                "max_tokens": 128,
            },
        )
        assert isinstance(result, Success), f"reasoning call failed: {error_detail(result)}"
        usage = result.body.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        think = details.get("reasoning_tokens")
        assert think is None or think == 0, f"reasoning_effort=none spent {think} think tokens"


class TestChatCompletions:
    def test_simple_chat_returns_content(self, gw: Gateway) -> None:
        key = _need_scoped(gw)
        result = gw.post(
            "/v1/chat/completions",
            key=key,
            json={
                "model": "Qwen/Qwen3-Next-80B-A3B-Instruct",
                "messages": [{"role": "user", "content": "Say hello in one word."}],
                "max_tokens": 32,
            },
        )
        assert isinstance(result, Success), f"chat failed: {error_detail(result)}"
        choices = result.body.get("choices")
        assert choices, "no choices"
        assert choices[0].get("message", {}).get("content"), "empty content"


class TestImageGeneration:
    def test_enhance_prompt_400(self, gw: Gateway) -> None:
        key = _need_scoped(gw)
        result = gw.post(
            "/v1/images/generations",
            key=key,
            json={"model": "Qwen/Qwen-Image-2.1", "prompt": "a red circle", "enhance_prompt": True},
        )
        assert result.status == 400, f"enhance_prompt should be 400, got {result.status}"
