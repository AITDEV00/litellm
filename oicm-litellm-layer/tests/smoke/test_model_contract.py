"""Tier B: model contract checks. Costs a small amount of inference.

Every request needs the scoped KEY (export KEY=sk-...). Deterministic where the
feature's contract is deterministic; envelope/shape elsewhere. Never asserts a
provider's output text or a latency number.
"""

from __future__ import annotations

import base64
import os

import pytest

from .client import Gateway, Success, error_detail

pytestmark = pytest.mark.model_contract

GETTYSBURG = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "tests", "proxy_unit_tests", "gettysburg.wav"
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
        assert "text" in result.body, "transcription missing 'text'"


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
        assert resp.content[:4] == b"RIFF", "tts response is not WAV (RIFF header missing)"
        assert len(resp.content) > 44, "tts WAV has no audio payload"


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
