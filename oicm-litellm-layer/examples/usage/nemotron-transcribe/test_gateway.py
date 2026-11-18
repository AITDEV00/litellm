#!/usr/bin/env python3
"""Exercise the public Nemotron Transcribe gateway contract with one WAV.

Authentication is read from LITELLM_API_KEY and never serialized into results.
POST output files contain response shapes, not raw audio or credentials.
Unsupported cases are recorded, not silently counted as successes.
"""
import base64
import json
import os
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("GATEWAY_BASE", "https://litellm.ecouncil.ae").rstrip("/")
MODEL = os.environ.get("NEMOTRON_MODEL", "nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B")
KEY = os.environ.get("LITELLM_API_KEY", "")
SSL = ssl.create_default_context() if BASE.startswith("https:") else None


def request(url, *, data=None, content_type=None, authenticated=False):
    headers = {}
    if content_type:
        headers["Content-Type"] = content_type
    if authenticated:
        if not KEY:
            raise ValueError("Export LITELLM_API_KEY for gateway examples")
        headers["Authorization"] = "Bearer " + KEY
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, data=data, headers=headers),
            timeout=180, context=SSL,
        ) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def multipart(fields, file_body):
    boundary = "nemotron-example"
    parts = []
    for name, value in fields.items():
        value = ("true" if value else "false") if isinstance(value, bool) else str(value)
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="audio.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode())
    parts.append(file_body)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), "multipart/form-data; boundary=" + boundary


def transcribe_chat(path, *, stream=False):
    audio = base64.b64encode(Path(path).read_bytes()).decode()
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "input_audio", "input_audio": {"data": audio, "format": "wav"}},
            {"type": "text", "text": "Transcribe the audio."},
        ]}],
        "stream": stream,
        **({"stream_options": {"include_usage": True}} if stream else {}),
    }).encode()
    return request(BASE + "/v1/chat/completions", data=body, content_type="application/json", authenticated=True)


def main():
    audio_path = os.environ.get("NEMOTRON_AUDIO")
    if not audio_path or not Path(audio_path).is_file():
        raise SystemExit("Set NEMOTRON_AUDIO to a readable source WAV")
    output = Path(os.environ.get("NEMOTRON_RESULTS", "./nemotron-contract-results.json"))
    cases = [
        ("json", {"model": MODEL, "response_format": "json"}),
        ("english-json", {"model": MODEL, "response_format": "json", "language": "en"}),
        ("word-timestamps", {"model": MODEL, "response_format": "verbose_json", "language": "en", "timestamp_granularities[]": "word"}),
        # Native text/srt/vtt and explicit stream are reported but are not
        # expected to be gateway equivalents in this deployment.
        ("text-comparison", {"model": MODEL, "response_format": "text"}),
        ("explicit-stream-comparison", {"model": MODEL, "stream": True}),
    ]
    results = {}
    for name, fields in cases:
        body, content_type = multipart(fields, Path(audio_path).read_bytes())
        started = time.perf_counter()
        status, raw = request(BASE + "/v1/audio/transcriptions", data=body, content_type=content_type, authenticated=True)
        record = {"status": status, "elapsed_s": time.perf_counter() - started, "content_type": "multipart"}
        try:
            parsed = json.loads(raw)
            record["keys"] = sorted(parsed)
            record["text"] = parsed.get("text")
            if name == "word-timestamps":
                record["word_count"] = len(parsed.get("words", []))
                record["language"] = parsed.get("language")
            if status == 200 and not str(parsed.get("text", "")).strip():
                record["error"] = "Missing non-empty transcript"
        except (UnicodeDecodeError, json.JSONDecodeError):
            text = raw.decode(errors="replace")
            record["raw_prefix"] = text[:500]
            if status == 200 and not text.strip():
                record["error"] = "Empty response"
        results["multipart-" + name] = record
        print("multipart-" + name, status, "text_ok", bool(record.get("text")), flush=True)
    for name, stream in [("chat", False), ("chat-stream", True)]:
        started = time.perf_counter()
        status, raw = transcribe_chat(audio_path, stream=stream)
        text = raw.decode(errors="replace")
        results[name] = {"status": status, "elapsed_s": time.perf_counter() - started, "stream": stream}
        if stream:
            text_parts = []
            done = False
            for line in text.splitlines():
                if not line.startswith("data: "):
                    continue
                data = line[6:].strip()
                if data == "[DONE]":
                    done = True
                    continue
                event = json.loads(data)
                for choice in event.get("choices", []):
                    fragment = choice.get("delta", {}).get("content")
                    if fragment:
                        text_parts.append(fragment)
            results[name]["content"] = "".join(text_parts)
            results[name]["done"] = done
            results[name]["error"] = None if done and results[name]["content"] else "Missing transcript or [DONE]"
        else:
            parsed = json.loads(text)
            results[name]["content"] = parsed.get("choices", [{}])[0].get("message", {}).get("content")
        print(name, status, "text_ok", bool(results[name].get("content")), flush=True)
    # Public route discovery; preserve status and body as evidence, never auth.
    for label, path, authenticated, expected_status in [
        ("model-list", "/v1/models", True, 200),
        ("translation-route", "/v1/audio/translations", True, 404),
        ("readiness", "/health/readiness", False, 200),
    ]:
        if label.endswith("route"):
            body, content_type = multipart({"model": MODEL}, Path(audio_path).read_bytes())
            status, raw = request(BASE + path, data=body, content_type=content_type, authenticated=authenticated)
        else:
            status, raw = request(BASE + path, authenticated=authenticated)
        results[label] = {"status": status, "expected_status": expected_status, "raw_prefix": raw.decode(errors="replace")[:800]}
        if status != expected_status:
            results[label]["error"] = "Status differs from previously validated contract"
        print(label, status, flush=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    if KEY:
        output.write_text(json.dumps(results, indent=2).replace(KEY, "[redacted]"))
    else:
        output.write_text(json.dumps(results, indent=2))
    required_ok = ["multipart-json", "multipart-english-json", "multipart-word-timestamps", "chat", "chat-stream", "model-list", "readiness"]
    unsupported_expected = {"translation-route": 404, "multipart-text-comparison": 500}
    errors = []
    for name in required_ok:
        if results[name].get("status") != 200 or results[name].get("error"):
            errors.append(name)
    if len(results["multipart-word-timestamps"].get("text", "")) == 0 or results["multipart-word-timestamps"].get("word_count", 0) < 1:
        errors.append("timestamp payload")
    if not results["chat"].get("content"):
        errors.append("chat content")
    for name, status in unsupported_expected.items():
        if results[name].get("status") != status:
            errors.append(name + " behavior changed; review before relying on matrix")
    print("Result file:", output)
    print("Failures:", errors)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
