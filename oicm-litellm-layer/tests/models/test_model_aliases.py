"""Exercise model_aliases.sh against a stateful localhost Admin API stub.

The real Bash CLI performs HTTP requests. Nothing is sent to a deployed proxy
or model. Assertions pin preservation, idempotency, safety, HTTP errors, and
verification so mutations of the editing logic turn into observable failures.
"""

import json
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "models" / "model_aliases.sh"
QWEN_NAME = "Qwen/Qwen3.6-35B-A3B-FP8"
QWEN_ALIAS = "Qwen3.6-35B-A3B-FP8"

for tool in ("bash", "curl", "jq"):
    if shutil.which(tool) is None:
        raise RuntimeError(f"{tool} is required to execute the Bash CLI tests")


@dataclass
class State:
    aliases: dict = field(default_factory=lambda: {"old-alias": {"model": "old-model", "hidden": True}})
    registered: list[str] = field(default_factory=lambda: [QWEN_NAME, "old-model"])
    source: str = "db"
    failure: str | None = None
    requests: list[dict] = field(default_factory=list)
    pending_aliases: dict | None = None
    verification_reads: int = 0


@pytest.fixture
def gateway():
    state = State()

    class Handler(BaseHTTPRequestHandler):
        def respond(self, body: object, status: int = 200):
            payload = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            state.requests.append({"method": "GET", "path": self.path,
                                   "auth": self.headers.get("Authorization")})
            if self.path == "/router/settings":
                state.verification_reads += 1
                aliases = state.pending_aliases if state.verification_reads == 0 else state.aliases
                self.respond({
                    "current_values": {"model_group_alias": aliases, "timeout": 99},
                    "source": {"model_group_alias": state.source},
                })
                return
            if self.path == "/model/info":
                self.respond({"data": [{"model_name": name} for name in state.registered]})
                return
            self.respond({"detail": "not found"}, 404)

        def do_POST(self):
            if self.path != "/config/update":
                self.respond({"detail": "not found"}, 404)
                return
            if self.headers.get("Expect", "").lower() == "100-continue":
                self.send_response_only(100)
                self.end_headers()
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            state.requests.append({"method": "POST", "path": self.path, "body": body,
                                   "auth": self.headers.get("Authorization")})
            if state.failure == "write_error":
                self.respond({"detail": "Only proxy admins"}, 403)
                return
            state.aliases = {} if state.failure == "diverge" else body["router_settings"]["model_group_alias"]
            self.respond({"message": "updated"})

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def cli(gateway, arguments, env=None):
    _, base_url = gateway
    environ = {"PROXY_BASE_URL": base_url, "LITELLM_API_KEY": "test-secret", "PATH": os.environ["PATH"]}
    environ.update(env or {})
    result = subprocess.run([str(SCRIPT), *arguments], env=environ, capture_output=True, text=True, check=False)
    assert "test-secret" not in result.stdout
    assert "test-secret" not in result.stderr
    return result


def posts(state):
    return [r for r in state.requests if r["method"] == "POST"]


def test_add_dry_run_preserves_aliases_and_does_not_write(gateway):
    state, _ = gateway
    result = cli(gateway, ["add", QWEN_ALIAS, QWEN_NAME])
    assert result.returncode == 0, result.stderr
    planned = json.loads(result.stdout.split("write:\n", maxsplit=1)[1])
    assert planned == {"router_settings": {"model_group_alias": {
        "old-alias": {"model": "old-model", "hidden": True}, QWEN_ALIAS: QWEN_NAME,
    }}}
    assert posts(state) == []


def test_add_apply_writes_and_verifies_the_actual_map(gateway):
    state, _ = gateway
    result = cli(gateway, ["add", QWEN_ALIAS, QWEN_NAME, "--apply"])
    assert result.returncode == 0, result.stderr
    assert "Applied and verified" in result.stdout
    assert "after 1 read(s)" in result.stdout
    assert state.aliases == {"old-alias": {"model": "old-model", "hidden": True}, QWEN_ALIAS: QWEN_NAME}
    assert posts(state) == [{"method": "POST", "path": "/config/update", "auth": "Bearer test-secret",
                              "body": {"router_settings": {"model_group_alias": state.aliases}}}]
    assert [r["path"] for r in state.requests] == [
        "/router/settings", "/model/info", "/config/update", "/router/settings",
    ]


def test_remove_preserves_all_unrelated_aliases(gateway):
    state, _ = gateway
    state.aliases = {QWEN_ALIAS: QWEN_NAME, "other": {"model": "old-model", "hidden": True}}
    result = cli(gateway, ["remove", QWEN_ALIAS, "--apply"])
    assert result.returncode == 0, result.stderr
    assert state.aliases == {"other": {"model": "old-model", "hidden": True}}
    assert posts(state)[0]["body"]["router_settings"]["model_group_alias"] == state.aliases


def test_hidden_add_and_repeat_have_expected_transition_and_noop(gateway):
    state, _ = gateway
    result = cli(gateway, ["add", QWEN_ALIAS, QWEN_NAME, "--hidden", "--apply"])
    assert result.returncode == 0, result.stderr
    expected = {"model": QWEN_NAME, "hidden": True}
    assert state.aliases[QWEN_ALIAS] == expected
    before = len(state.requests)
    second = cli(gateway, ["add", QWEN_ALIAS, QWEN_NAME, "--hidden", "--apply"])
    assert second.returncode == 0, second.stderr
    assert "No change" in second.stdout
    assert state.aliases[QWEN_ALIAS] == expected
    assert not any(r["method"] == "POST" for r in state.requests[before:])


@pytest.mark.parametrize("arguments, aliases_value, source, expected", [
    (["add", QWEN_ALIAS, f"hosted_vllm/{QWEN_NAME}"], None, None, "not a registered model"),
    (["add", "old-model", "Qwen/Qwen3.6-35B-A3B-FP8"], None, None, "refusing to shadow"),
    (["remove", "old-model"], None, None, "refusing to shadow"),
    (["add", QWEN_ALIAS, QWEN_NAME], {QWEN_ALIAS: "old-model"}, None, "already targets"),
    (["add", QWEN_ALIAS, QWEN_NAME], None, "config", "owned by config.yaml"),
    (["remove", QWEN_ALIAS, "--hidden"], None, None, "only valid for add"),
])
def test_invalid_or_config_owned_inputs_never_write(gateway, arguments, aliases_value, source, expected):
    state, _ = gateway
    if aliases_value is not None:
        state.aliases = aliases_value
    if source is not None:
        state.source = source
    result = cli(gateway, arguments)
    assert result.returncode != 0
    assert expected in result.stderr
    assert posts(state) == []


def test_apply_verification_failure_does_not_claim_success(gateway):
    state, _ = gateway
    state.failure = "diverge"
    result = cli(gateway, ["add", QWEN_ALIAS, QWEN_NAME, "--apply"], env={"LITELLM_VERIFY_DELAY_SECONDS": "0"})
    assert result.returncode != 0
    assert "different alias map during verification" in result.stderr


def test_apply_verification_waits_for_eventual_consistency(gateway):
    state, _ = gateway
    state.pending_aliases = {"old-alias": {"model": "old-model", "hidden": True}}
    result = cli(gateway, ["add", QWEN_ALIAS, QWEN_NAME, "--apply"], env={"LITELLM_VERIFY_DELAY_SECONDS": "0"})
    assert result.returncode == 0, result.stderr
    assert "Applied and verified" in result.stdout
    assert "after 2 read(s)" in result.stdout
    assert state.aliases[QWEN_ALIAS] == QWEN_NAME


def test_apply_verification_does_not_sleep_after_a_successful_first_read(gateway):
    result = cli(gateway, ["add", QWEN_ALIAS, QWEN_NAME, "--apply"], env={"LITELLM_VERIFY_DELAY_SECONDS": "99"})
    assert result.returncode == 0, result.stderr
    assert "after 2 read(s)" not in result.stdout


def test_verification_delay_must_be_a_finite_nonnegative_number(gateway):
    result = cli(gateway, ["list"], env={"LITELLM_VERIFY_DELAY_SECONDS": "-1"})
    assert result.returncode != 0
    assert "LITELLM_VERIFY_DELAY_SECONDS" in result.stderr


def test_gateway_error_is_visible_and_not_silent(gateway):
    state, _ = gateway
    state.failure = "write_error"
    result = cli(gateway, ["add", QWEN_ALIAS, QWEN_NAME, "--apply"])
    assert result.returncode != 0
    assert "HTTP 403" in result.stderr
    assert "Only proxy admins" in result.stderr


def test_missing_credential_and_transport_failure_are_handled(gateway):
    state, _ = gateway
    missing = cli(gateway, ["list"], env={"LITELLM_API_KEY": ""})
    assert missing.returncode != 0
    assert "set LITELLM_API_KEY" in missing.stderr
    assert state.requests == []
    failed = cli(gateway, ["list"], env={"PROXY_BASE_URL": "http://127.0.0.1:1", "GATEWAY_TIMEOUT_SECONDS": "0.1"})
    assert failed.returncode != 0
    assert "HTTP 000" not in failed.stderr


def test_remove_absent_alias_is_noop(gateway):
    state, _ = gateway
    result = cli(gateway, ["remove", QWEN_ALIAS, "--apply"])
    assert result.returncode == 0, result.stderr
    assert "No change" in result.stdout
    assert posts(state) == []
    assert state.aliases == {"old-alias": {"model": "old-model", "hidden": True}}
