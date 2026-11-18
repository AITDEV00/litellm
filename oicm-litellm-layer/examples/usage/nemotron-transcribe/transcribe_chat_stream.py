#!/usr/bin/env python3
"""Stream base64 WAV through Nemotron audio-in-chat and reconstruct its text."""
import base64
import json
import os
import ssl
import sys
import urllib.request

BASE = os.environ.get("GATEWAY_BASE", "https://litellm.ecouncil.ae").rstrip("/")
MODEL = os.environ.get(
    "NEMOTRON_MODEL", "nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B"
)
AUDIO = os.environ.get("NEMOTRON_AUDIO")
KEY = os.environ.get("LITELLM_API_KEY", "")
if not AUDIO:
    raise SystemExit("Export NEMOTRON_AUDIO to a readable WAV path")
if not KEY:
    raise SystemExit("Export LITELLM_API_KEY locally; do not hard-code it")
with open(AUDIO, "rb") as source:
    b64 = base64.b64encode(source.read()).decode()
payload = {
    "model": MODEL,
    "messages": [{"role": "user", "content": [
        {"type": "input_audio", "input_audio": {"data": b64, "format": "wav"}},
        {"type": "text", "text": "Transcribe the audio."},
    ]}],
    "stream": True,
    "stream_options": {"include_usage": True},
}
headers = {"Content-Type": "application/json", "Authorization": "Bearer " + KEY}
request = urllib.request.Request(
    BASE + "/v1/chat/completions", data=json.dumps(payload).encode(), headers=headers
)
text_parts = []
done = False
with urllib.request.urlopen(request, timeout=180, context=ssl.create_default_context()) as response:
    if response.getheader("Content-Type", "").split(";")[0] != "text/event-stream":
        raise RuntimeError(f"Expected SSE: HTTP {response.status}, "
                           f"Content-Type {response.getheader('Content-Type')}")
    for line in response:
        if not line.startswith(b"data: "):
            continue
        data = line[6:].strip()
        if data == b"[DONE]":
            done = True
            continue
        event = json.loads(data)
        if event.get("usage"):
            print("\nUsage:", json.dumps(event["usage"]), file=sys.stderr)
        for choice in event.get("choices", []):
            fragment = choice.get("delta", {}).get("content")
            if fragment:
                text_parts.append(fragment)
                print(fragment, end="", flush=True)
if not done:
    raise RuntimeError("SSE stream ended missing the terminal marker")
if not "".join(text_parts).strip():
    raise RuntimeError("Stream completed with no transcript text")
print()
