"""Tier A: contract checks with no model inference.

Each group maps to a documented feature. Assert status + body contract, then
clean up via ``resources``. Every test fails loudly when the gateway or a
required key is missing; nothing skips.

Master key runs as proxy_admin. The scoped key (KEY) is used wherever a test
must prove a customer key's behavior (model access 403, inference). A missing
scoped key fails those tests, not skips them.
"""

from __future__ import annotations

import os
import uuid

import pytest

from .client import Gateway, Resources, Success, error_detail

pytestmark = pytest.mark.contract

RUN = uuid.uuid4().hex[:8]


def _need_scoped(gw: Gateway) -> str:
    if not gw.scoped_key:
        pytest.fail("KEY not set: export a scoped llm_api key to run this check")
    return gw.scoped_key


# ---------------------------------------------------------------------------
# Health + debug envelope
# ---------------------------------------------------------------------------


class TestHealthAndDebug:
    def test_liveness(self, gw: Gateway) -> None:
        result = gw.get("/health/liveness")
        assert isinstance(result, Success), f"liveness failed: {error_detail(result)}"

    def test_readiness(self, gw: Gateway) -> None:
        result = gw.get("/health/readiness")
        assert isinstance(result, Success), f"readiness failed: {error_detail(result)}"

    def test_memory_summary_envelope(self, gw: Gateway) -> None:
        result = gw.get("/debug/memory/summary")
        assert isinstance(result, Success), f"memory summary failed: {error_detail(result)}"
        assert "memory" in result.body, "summary missing 'memory' key"
        assert "caches" in result.body, "summary missing 'caches' key"

    def test_asyncio_tasks_envelope(self, gw: Gateway) -> None:
        result = gw.get("/debug/asyncio-tasks")
        assert isinstance(result, Success), f"asyncio-tasks failed: {error_detail(result)}"
        assert isinstance(result.body.get("total_active_tasks"), int), "missing total_active_tasks"
        assert isinstance(result.body.get("by_name"), dict), "missing by_name"

    def test_metrics_exposed(self, gw: Gateway) -> None:
        result = gw.get("/metrics")
        assert isinstance(result, Success), f"/metrics failed: {error_detail(result)}"


# ---------------------------------------------------------------------------
# Admin CRUD round-trips
# ---------------------------------------------------------------------------


class TestKeyCrud:
    def test_key_create_read_delete(self, gw: Gateway, resources: Resources) -> None:
        alias = f"smoke-{RUN}-key"
        created = gw.post("/key/generate", json={"key_alias": alias, "key_type": "llm_api"})
        assert isinstance(created, Success), f"key generate failed: {error_detail(created)}"
        key = created.body.get("key")
        assert key, "key generate returned no 'key'"
        resources.defer(lambda: gw.post("/key/delete", json={"keys": [key]}))

        info = gw.get("/key/info", params={"key": key})
        assert isinstance(info, Success), f"key info failed: {error_detail(info)}"
        assert info.body.get("info", {}).get("key_alias") == alias

        deleted = gw.post("/key/delete", json={"keys": [key]})
        assert isinstance(deleted, Success), f"key delete failed: {error_detail(deleted)}"


class TestTeamCrud:
    def test_team_create_read_delete(self, gw: Gateway, resources: Resources) -> None:
        alias = f"smoke-{RUN}-team"
        created = gw.post("/team/new", json={"team_alias": alias})
        assert isinstance(created, Success), f"team new failed: {error_detail(created)}"
        team_id = created.body.get("team_id")
        assert team_id, "team new returned no 'team_id'"
        resources.defer(lambda: gw.post("/team/delete", json={"team_ids": [team_id]}))

        info = gw.get("/team/info", params={"team_id": team_id})
        assert isinstance(info, Success), f"team info failed: {error_detail(info)}"
        assert info.body.get("team_info", {}).get("team_alias") == alias


class TestBudgetCrud:
    def test_budget_create_delete(self, gw: Gateway, resources: Resources) -> None:
        budget_id = f"smoke-{RUN}-budget"
        created = gw.post("/budget/new", json={"budget_id": budget_id, "max_budget": 1.0})
        assert isinstance(created, Success), f"budget new failed: {error_detail(created)}"
        resources.defer(lambda: gw.post("/budget/delete", json={"id": budget_id}))

        deleted = gw.post("/budget/delete", json={"id": budget_id})
        assert isinstance(deleted, Success), f"budget delete failed: {error_detail(deleted)}"


# ---------------------------------------------------------------------------
# Authorization matrix
# ---------------------------------------------------------------------------


class TestAuthMatrix:
    def test_no_key_is_401(self, gw: Gateway) -> None:
        result = gw.request("GET", "/v1/models", key="")
        assert result.status == 401, f"no key should be 401, got {result.status}"

    def test_bad_key_is_401(self, gw: Gateway) -> None:
        result = gw.request("GET", "/v1/models", key="sk-not-a-real-key")
        assert result.status == 401, f"bad key should be 401, got {result.status}"

    def test_master_key_is_admin(self, gw: Gateway) -> None:
        result = gw.get("/key/list", params={"page": 1, "size": 1})
        assert isinstance(result, Success), f"master key cannot list keys: {error_detail(result)}"


# ---------------------------------------------------------------------------
# Voice / OmniVoice read endpoints (free, no inference)
# ---------------------------------------------------------------------------


class TestVoiceReadEndpoints:
    @pytest.mark.parametrize(
        "path",
        [
            "/v1/audio/health",
            "/v1/audio/metrics",
            "/v1/audio/models",
            "/v1/voices",
            "/v1/voices/profiles",
        ],
    )
    def test_get_endpoint_reachable(self, gw: Gateway, path: str) -> None:
        result = gw.get(path)
        # A connection failure is status 0, which is < 500, so guard it
        # explicitly: a dead gateway must fail, not false-pass "reachable".
        assert isinstance(result, Success), (
            f"{path} unreachable or errored: status={result.status} {error_detail(result)}"
        )
        assert result.status != 404, f"{path} returned 404 (router dropped?)"


class TestVoiceProfileCrud:
    def test_profile_create_read_delete(self, gw: Gateway, resources: Resources) -> None:
        # Voice-profile create is a voice clone: it requires ref_audio as a
        # multipart file (see the OmniVoice guide). Use the real audio fixture;
        # a 0-frame synthetic WAV fails backend validation with "0 frames".
        fixture = os.path.join(
            os.path.dirname(__file__), "..", "..", "..", "tests", "proxy_unit_tests", "gettysburg.wav"
        )
        assert os.path.exists(fixture), f"audio fixture missing: {fixture}"
        alias = f"smoke-{RUN}-profile"
        with open(fixture, "rb") as fh:
            created = gw.post(
                "/v1/voices/profiles",
                files={"ref_audio": ("ref.wav", fh, "audio/wav")},
                data={"profile_id": alias},
            )
        assert created.status != 404, "voice profile create 404 (voice router dropped?)"
        assert isinstance(created, Success), f"profile create failed: {error_detail(created)}"
        profile_id = created.body.get("profile_id") or created.body.get("id") or created.body.get("voice_id") or alias
        resources.defer(lambda: gw.delete(f"/v1/voices/profiles/{profile_id}"))

        detail = gw.get(f"/v1/voices/profiles/{profile_id}")
        assert detail.status != 404, "created profile not readable"


# ---------------------------------------------------------------------------
# Error contracts from the usage guides
# ---------------------------------------------------------------------------


class TestErrorContracts:
    def test_tts_unknown_voice_400(self, gw: Gateway) -> None:
        key = _need_scoped(gw)
        result = gw.post(
            "/v1/audio/speech",
            key=key,
            json={"model": "hamsa-tts-new", "input": "hello", "voice": "not-a-real-voice-xyz"},
        )
        assert result.status == 400, f"unknown voice should be 400, got {result.status}"
        assert "not loaded" in error_detail(result).lower() or "speaker" in error_detail(result).lower()

    def test_diarize_unsupported_field_400(self, gw: Gateway) -> None:
        key = _need_scoped(gw)
        import io

        wav = io.BytesIO(b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00")
        result = gw.post(
            "/v1/diarize",
            key=key,
            files={"file": ("a.wav", wav, "audio/wav")},
            data={"model": "pyannote/community-1", "numSpeakers": "2"},
        )
        assert result.status in (400, 422), f"unsupported diarize field should be 400/422, got {result.status}"

    def test_multipart_metadata_lone_surrogate_400(self, gw: Gateway) -> None:
        """v1.106.6: a lone surrogate escape in a multipart metadata field must
        400 the same way the JSON body path does (no provider or DB can encode
        it). This pins the _parse_form_metadata hardening in
        litellm/proxy/common_utils/http_parsing_utils.py."""
        key = _need_scoped(gw)
        import io

        wav = io.BytesIO(b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00")
        result = gw.post(
            "/v1/diarize",
            key=key,
            files={"file": ("a.wav", wav, "audio/wav")},
            data={"model": "pyannote/community-1", "metadata": '{"surrogate": "\\ud800"}'},
        )
        assert result.status == 400, (
            f"lone surrogate in multipart metadata should be 400, got {result.status}: {error_detail(result)}"
        )


# ---------------------------------------------------------------------------
# /v1/models scoping
# ---------------------------------------------------------------------------


class TestModelListing:
    def test_master_key_lists_models(self, gw: Gateway) -> None:
        result = gw.get("/v1/models")
        assert isinstance(result, Success), f"/v1/models failed: {error_detail(result)}"
        assert isinstance(result.body.get("data"), list), "model list missing 'data'"

    def test_scoped_key_subset_of_master(self, gw: Gateway) -> None:
        key = _need_scoped(gw)
        master = gw.get("/v1/models")
        scoped = gw.get("/v1/models", key=key)
        assert isinstance(master, Success) and isinstance(scoped, Success)
        master_ids = {m["id"] for m in master.body["data"]}
        scoped_ids = {m["id"] for m in scoped.body["data"]}
        assert scoped_ids <= master_ids, "scoped key sees models master does not"

    def test_chat_models_advertise_a_usable_token_budget(self, gw: Gateway) -> None:
        """Every chat row must describe a real completion budget, never the
        raw context window in every field.

        The gateway's own /v1/models discovery fills unset token fields with
        max_model_len; a client that adopts max_output_tokens as its request
        default then overflows prompt + max_tokens on every request, so a
        one-word chat 400s with a misleading "conversation too long" error.
        The discovery controller stamps a split so this holds on any gateway
        it manages; this pins it so a regression fails the smoke suite.
        """
        result = gw.get("/model/info")
        assert isinstance(result, Success), f"/model/info failed: {error_detail(result)}"
        problems = []
        for item in result.body.get("data", []):
            info = item.get("model_info", {})
            if info.get("mode") != "chat":
                continue
            name = item.get("model_name", "?")
            context = info.get("context_length") or info.get("max_input_tokens")
            output = info.get("max_output_tokens")
            inp = info.get("max_input_tokens")
            legacy = info.get("max_tokens")
            if output is None and context is None and legacy is None:
                # A row that advertises no token data at all (an image-gen or
                # compute surface that fell into the chat default) was never
                # part of the outage: clients fall back to their own
                # conservative, capped guess for it.
                continue
            if output is None or context is None:
                problems.append(f"{name}: partial token metadata (ctx={context}, out={output})")
                continue
            if output >= context:
                problems.append(f"{name}: output {output} >= window {context}")
            if inp is not None and inp + output > context:
                problems.append(f"{name}: in+out {inp + output} > window {context}")
            if legacy is not None and legacy >= context:
                problems.append(f"{name}: legacy max_tokens {legacy} >= window {context}")
        assert not problems, "chat models misreporting token budgets:\n" + "\n".join(problems)


# ---------------------------------------------------------------------------
# /model/performance + /model/metrics
# ---------------------------------------------------------------------------


class TestModelPerformance:
    def test_live_window_uses_prometheus(self, gw: Gateway) -> None:
        result = gw.get("/model/performance", params={"window": "1h"})
        assert isinstance(result, Success), f"/model/performance failed: {error_detail(result)}"
        assert result.body.get("source") == "prometheus", f"1h window source != prometheus: {result.body.get('source')}"

    def test_invalid_window_400(self, gw: Gateway) -> None:
        result = gw.get("/model/performance", params={"window": "bogus"})
        assert result.status == 400, f"bogus window should be 400, got {result.status}"

    def test_per_model_metrics(self, gw: Gateway) -> None:
        result = gw.get("/model/metrics/per_model", params={"window": "1h"})
        assert isinstance(result, Success), f"per_model metrics failed: {error_detail(result)}"


# ---------------------------------------------------------------------------
# Client distribution
# ---------------------------------------------------------------------------


class TestClientDistribution:
    def test_install_script_served(self, gw: Gateway) -> None:
        result = gw.get("/install")
        assert isinstance(result, Success), f"/install unreachable or errored: status={result.status} {error_detail(result)}"
        assert result.status != 404, "/install 404 (client_distribution router dropped?)"

    def test_install_manifest(self, gw: Gateway) -> None:
        result = gw.get("/install/manifest.json")
        assert isinstance(result, Success), f"manifest unreachable or errored: status={result.status} {error_detail(result)}"
        assert result.status != 404, "manifest 404"
        assert isinstance(result.body, (dict, str)), "manifest not JSON/text"


# ---------------------------------------------------------------------------
# OICM status API
# ---------------------------------------------------------------------------


class TestOicmStatus:
    def test_status_reports_requires_admin(self, gw: Gateway) -> None:
        result = gw.post("/oicm/v1/status-reports", key="", json={"reporter": "smoke", "reports": []})
        assert result.status == 401, f"status-reports without key should be 401, got {result.status}"

    def test_status_reports_admin_write(self, gw: Gateway) -> None:
        # An admin write succeeds and is idempotent. This also proves the route
        # is not just "always 401" (which the negative test alone would accept).
        result = gw.post(
            "/oicm/v1/status-reports",
            json={
                "reporter": "smoke",
                "reports": [
                    {
                        "model_name": f"smoke-{RUN}",
                        "litellm_model_id": None,
                        "healthy": True,
                        "error_message": None,
                        "details": None,
                    }
                ],
            },
        )
        assert isinstance(result, Success), f"admin status-report write failed: {error_detail(result)}"


# ---------------------------------------------------------------------------
# Router settings
# ---------------------------------------------------------------------------


class TestRouterSettings:
    def test_get_router_settings(self, gw: Gateway) -> None:
        result = gw.get("/router/settings")
        assert isinstance(result, Success), f"/router/settings failed: {error_detail(result)}"

    def test_router_fields(self, gw: Gateway) -> None:
        result = gw.get("/router/fields")
        assert isinstance(result, Success), f"/router/fields failed: {error_detail(result)}"
