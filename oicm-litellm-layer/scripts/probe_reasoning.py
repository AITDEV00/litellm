#!/usr/bin/env python3
"""Probe reasoning control on the ADEO LiteLLM gateway.

Every request carries a unique nonce so LiteLLM's response cache cannot serve a
stale answer for a different probe, a hard timeout, and a small output cap, so
the whole matrix stays cheap to re-run while hypotheses are being iterated.

`reasoning_tokens` is reported alongside `reasoning_content` because a model can
spend thinking tokens that never surface in the response body; the two signals
disagreeing is what distinguishes "thinking disabled" from "thinking stripped".

Usage:
    python3 scripts/probe_reasoning.py --list
    python3 scripts/probe_reasoning.py --model Kimi
    python3 scripts/probe_reasoning.py --label glm
    python3 scripts/probe_reasoning.py --full --label kimi/none
    python3 scripts/probe_reasoning.py --repeat 3 --probe 'tag|model|{"reasoning_effort":"low"}'
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Final, Mapping, Sequence

DEFAULT_BASE_URL: Final = "https://litellm.ecouncil.ae"
DEFAULT_TIMEOUT_S: Final = 10.0
DEFAULT_MAX_TOKENS: Final = 129
DEFAULT_PROMPT: Final = "What is 15% of 240?"

KIMI: Final = "moonshotai/Kimi-K3"
GLM: Final = "zai-org/GLM-5.3-Flash"
GLM_53: Final = "zai-org/GLM-5.3"
QWEN: Final = "Qwen/Qwen3.8-Flash-Next-FP8"


@dataclass(frozen=True, slots=True)
class Probe:
    label: str
    model: str
    params: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Config:
    base_url: str
    api_key: str
    timeout_s: float
    max_tokens: int
    prompt: str
    ssl_context: ssl.SSLContext


@dataclass(frozen=True, slots=True)
class Result:
    probe: Probe
    status: int
    elapsed_s: float
    response_id: str | None
    reasoning_content: str | None
    reasoning_tokens: int | None
    content: str | None
    error: str | None


def _effort_sweep(model: str, tag: str, levels: Sequence[str]) -> tuple[Probe, ...]:
    return tuple(
        Probe(label=f"{tag}/effort={level}", model=model, params={"reasoning_effort": level})
        for level in levels
    )


def _thinking_sweep(model: str, tag: str) -> tuple[Probe, ...]:
    return (
        Probe(label=f"{tag}/baseline", model=model),
        Probe(label=f"{tag}/thinking=false", model=model, params={"thinking": False}),
        Probe(
            label=f"{tag}/extra_body.chat_template_kwargs.thinking=false",
            model=model,
            params={"extra_body": {"chat_template_kwargs": {"thinking": False}}},
        ),
        Probe(
            label=f"{tag}/chat_template_kwargs.thinking=false",
            model=model,
            params={"chat_template_kwargs": {"thinking": False}},
        ),
        Probe(
            label=f"{tag}/extra_body.chat_template_kwargs.enable_thinking=false",
            model=model,
            params={"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}},
        ),
    )


PROBES: Final[tuple[Probe, ...]] = (
    *_thinking_sweep(KIMI, "kimi"),
    *_effort_sweep(KIMI, "kimi", ("low", "high", "max", "none", "minimal", "medium")),
    *_thinking_sweep(GLM, "glm"),
    *_effort_sweep(GLM, "glm", ("low", "high", "none", "minimal", "medium")),
    *_thinking_sweep(GLM_53, "glm53"),
    *_effort_sweep(GLM_53, "glm53", ("low", "high", "none", "minimal", "medium", "xhigh", "max")),
    *_thinking_sweep(QWEN, "qwen"),
    *_effort_sweep(QWEN, "qwen", ("low", "medium", "xhigh", "none", "minimal")),
)


def _ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _build_body(probe: Probe, config: Config, nonce: str) -> bytes:
    body = {
        "model": probe.model,
        "messages": [{"role": "user", "content": f"{config.prompt} [ref:{nonce}]"}],
        "max_tokens": config.max_tokens,
        **probe.params,
    }
    return json.dumps(body).encode()


def _extract(
    payload: Mapping[str, object],
) -> tuple[str | None, str | None, int | None, str | None, str | None]:
    response_id = payload.get("id")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        error = payload.get("error")
        return (
            response_id if isinstance(response_id, str) else None,
            None,
            None,
            None,
            json.dumps(error)[:400] if error else "no choices in response",
        )
    first = choices[0]
    if not isinstance(first, dict):
        return None, None, None, None, "malformed choice"
    message = first.get("message")
    if not isinstance(message, dict):
        return None, None, None, None, "malformed message"
    reasoning_content = message.get("reasoning_content")
    content = message.get("content")
    usage = payload.get("usage")
    details = usage.get("completion_tokens_details") if isinstance(usage, dict) else None
    reasoning_tokens = details.get("reasoning_tokens") if isinstance(details, dict) else None
    return (
        response_id if isinstance(response_id, str) else None,
        reasoning_content if isinstance(reasoning_content, str) else None,
        reasoning_tokens if isinstance(reasoning_tokens, int) else None,
        content if isinstance(content, str) else None,
        None,
    )


def run_probe(probe: Probe, config: Config) -> Result:
    nonce = f"{time.time_ns():x}{secrets.token_hex(4)}"
    request = urllib.request.Request(
        f"{config.base_url}/v1/chat/completions",
        data=_build_body(probe, config, nonce),
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(
            request, timeout=config.timeout_s, context=config.ssl_context
        ) as response:
            status = response.status
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return Result(
            probe=probe,
            status=exc.code,
            elapsed_s=time.monotonic() - started,
            response_id=None,
            reasoning_content=None,
            reasoning_tokens=None,
            content=None,
            error=exc.read().decode(errors="replace")[:400],
        )
    except Exception as exc:
        return Result(
            probe=probe,
            status=0,
            elapsed_s=time.monotonic() - started,
            response_id=None,
            reasoning_content=None,
            reasoning_tokens=None,
            content=None,
            error=f"{type(exc).__name__}: {exc}",
        )
    if not isinstance(payload, dict):
        return Result(
            probe=probe,
            status=status,
            elapsed_s=time.monotonic() - started,
            response_id=None,
            reasoning_content=None,
            reasoning_tokens=None,
            content=None,
            error="non-object JSON response",
        )
    response_id, reasoning_content, reasoning_tokens, content, error = _extract(payload)
    return Result(
        probe=probe,
        status=status,
        elapsed_s=time.monotonic() - started,
        response_id=response_id,
        reasoning_content=reasoning_content,
        reasoning_tokens=reasoning_tokens,
        content=content,
        error=error,
    )


def _verdict(result: Result) -> str:
    if result.error is not None:
        return "ERR"
    has_block = bool(result.reasoning_content)
    spent = result.reasoning_tokens or 0
    if has_block:
        return "THINK"
    return "THINK-STRIPPED" if spent > 0 else "NO-THINK"


def _is_cache_hit(result: Result, seen: frozenset[str]) -> bool:
    return result.response_id is not None and result.response_id in seen


def _preview(text: str | None, limit: int | None) -> str:
    if not text:
        return "-"
    collapsed = " ".join(text.split())
    if limit is None or len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3] + "..."


def _render(results: Sequence[Result], full: bool) -> None:
    if not results:
        print("no probes matched the filters")
        return
    seen: set[str] = set()
    cache_hits = 0
    label_width = max(len(result.probe.label) for result in results)
    print(
        f"{'probe'.ljust(label_width)}  {'st':>4}  {'sec':>5}  {'rc_ch':>6}  "
        f"{'think_tok':>9}  {'content_ch':>10}  cache  verdict"
    )
    print("-" * (label_width + 61))
    for result in results:
        rc_chars = len(result.reasoning_content) if result.reasoning_content else 0
        content_chars = len(result.content) if result.content else 0
        think_tok = result.reasoning_tokens if result.reasoning_tokens is not None else "-"
        cached = _is_cache_hit(result, frozenset(seen))
        if cached:
            cache_hits += 1
        if result.response_id is not None:
            seen.add(result.response_id)
        print(
            f"{result.probe.label.ljust(label_width)}  {result.status:>4}  "
            f"{result.elapsed_s:>5.1f}  {rc_chars:>6}  {str(think_tok):>9}  "
            f"{content_chars:>10}  {'HIT' if cached else 'miss':>5}  {_verdict(result)}"
        )
    print()
    if cache_hits:
        print(
            f"WARNING: {cache_hits} probe(s) reused a response id already seen this run, "
            "so those rows are not fresh generations\n"
        )
    limit = None if full else 160
    for result in results:
        print(f"[{result.probe.label}]")
        if result.error is not None:
            print(f"  error: {_preview(result.error, 400)}")
        else:
            print(f"  reasoning_content: {_preview(result.reasoning_content, limit)}")
            print(f"  content: {_preview(result.content, limit)}")
        print()


def _base_label(label: str) -> str:
    head, sep, tail = label.rpartition("#")
    return head if sep and tail.isdigit() else label


def _summarize(results: Sequence[Result]) -> None:
    """Collapse repeat runs into one consensus row per probe."""
    if not results:
        print("no probes matched the filters")
        return
    grouped: dict[str, list[Result]] = {}
    for result in results:
        grouped.setdefault(_base_label(result.probe.label), []).append(result)
    label_width = max(len(label) for label in grouped)
    print(
        f"{'probe'.ljust(label_width)}  {'n':>3}  {'think_tok':>9}  {'rc_ch':>6}  "
        f"{'content_ch':>10}  consensus"
    )
    print("-" * (label_width + 50))
    for label, group in grouped.items():
        verdicts = tuple(_verdict(result) for result in group)
        counts = {verdict: verdicts.count(verdict) for verdict in dict.fromkeys(verdicts)}
        if len(counts) == 1:
            consensus = verdicts[0]
        else:
            consensus = "MIXED " + " ".join(f"{k}x{v}" for k, v in counts.items())
        tokens = [r.reasoning_tokens for r in group if r.reasoning_tokens is not None]
        rc_chars = [len(r.reasoning_content) for r in group if r.reasoning_content]
        content_chars = [len(r.content) for r in group if r.content]
        mean_tokens = f"{sum(tokens) / len(tokens):.0f}" if tokens else "-"
        mean_rc = f"{sum(rc_chars) / len(rc_chars):.0f}" if rc_chars else "0"
        mean_content = f"{sum(content_chars) / len(content_chars):.0f}" if content_chars else "0"
        print(
            f"{label.ljust(label_width)}  {len(group):>3}  {mean_tokens:>9}  "
            f"{mean_rc:>6}  {mean_content:>10}  {consensus}"
        )
    print()
    print("mean think_tok is over runs that reported a count; '-' means no run did")
    print("THINK-STRIPPED means thinking ran but the block was withheld from the response")
    print()


def _select(probes: Sequence[Probe], models: Sequence[str], labels: Sequence[str]) -> tuple[Probe, ...]:
    return tuple(
        probe
        for probe in probes
        if (not models or any(token.lower() in probe.model.lower() for token in models))
        and (not labels or any(token.lower() in probe.label.lower() for token in labels))
    )


def _ad_hoc_probes(specs: Sequence[str]) -> tuple[Probe, ...]:
    """Parse `label|model|json-params` specs into probes without editing the file."""
    probes: list[Probe] = []
    for spec in specs:
        parts = spec.split("|", 2)
        if len(parts) < 2:
            raise SystemExit(f"bad --probe spec (want label|model|json): {spec}")
        label, model = parts[0], parts[1]
        raw_params = parts[2] if len(parts) == 3 else ""
        if not label or not model:
            raise SystemExit(f"bad --probe spec (want label|model|json): {spec}")
        parsed = json.loads(raw_params) if raw_params else {}
        if not isinstance(parsed, dict):
            raise SystemExit(f"--probe params must be a JSON object: {spec}")
        probes.append(Probe(label=label, model=model, params=parsed))
    return tuple(probes)


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default=os.environ.get("PROXY_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--api-key", default=os.environ.get("LITELLM_API_KEY", ""))
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--model", action="append", default=[], help="substring filter, repeatable")
    parser.add_argument("--label", action="append", default=[], help="substring filter, repeatable")
    parser.add_argument(
        "--probe",
        action="append",
        default=[],
        help="ad-hoc probe as label|model|json-params, repeatable (skips the built-in matrix)",
    )
    parser.add_argument("--repeat", type=int, default=1, help="run each probe N times")
    parser.add_argument("--list", action="store_true", help="print probe labels and exit")
    parser.add_argument("--json", action="store_true", help="emit raw results as JSON")
    parser.add_argument("--summary", action="store_true", help="aggregate repeats into one row per probe")
    parser.add_argument("--full", action="store_true", help="do not truncate previews")
    return parser.parse_args(argv)


def main(argv: Sequence[str]) -> int:
    args = _parse_args(argv)
    selected = (
        _ad_hoc_probes(args.probe)
        if args.probe
        else _select(PROBES, args.model, args.label)
    )
    if args.list:
        for probe in selected:
            print(f"{probe.label}\t{probe.model}")
        return 0
    if not args.api_key:
        print("missing API key: pass --api-key or set LITELLM_API_KEY", file=sys.stderr)
        return 2
    config = Config(
        base_url=args.base_url.rstrip("/"),
        api_key=args.api_key,
        timeout_s=args.timeout,
        max_tokens=args.max_tokens,
        prompt=args.prompt,
        ssl_context=_ssl_context(),
    )
    if args.repeat < 1:
        print("--repeat must be >= 1", file=sys.stderr)
        return 2
    repeated = tuple(
        Probe(label=probe.label if args.repeat == 1 else f"{probe.label}#{index + 1}", model=probe.model, params=probe.params)
        for probe in selected
        for index in range(args.repeat)
    )
    results = tuple(run_probe(probe, config) for probe in repeated)
    if args.json:
        json.dump(
            [
                {
                    "label": result.probe.label,
                    "model": result.probe.model,
                    "params": dict(result.probe.params),
                    "status": result.status,
                    "elapsed_s": round(result.elapsed_s, 3),
                    "verdict": _verdict(result),
                    "reasoning_tokens": result.reasoning_tokens,
                    "reasoning_content": result.reasoning_content,
                    "content": result.content,
                    "error": result.error,
                }
                for result in results
            ],
            sys.stdout,
            indent=2,
        )
        print()
        return 0
    if args.summary:
        _summarize(results)
        return 0
    _render(results, args.full)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
