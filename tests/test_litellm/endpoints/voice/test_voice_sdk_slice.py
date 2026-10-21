"""Regression tests for the OICM custom voice/script SDK slice.

These functions were historically grafted into ``litellm.main`` and got dropped
in upstream merges (the exact bug that hit the voice SDK after the v1.97.0
merge). They now live in the co-located ``litellm.endpoints.voice`` slice and
are re-exported lazily through ``litellm``. These tests assert the public
``litellm.*`` API keeps resolving so a merge that deletes the slice (or the
re-export) fails loudly instead of silently breaking the voice feature.
"""

import pytest

import litellm


@pytest.mark.parametrize(
    "name,expected_module",
    [
        ("create_voice", "litellm.endpoints.voice.main"),
        ("acreate_voice", "litellm.endpoints.voice.main"),
        ("script", "litellm.endpoints.voice.main"),
        ("ascript", "litellm.endpoints.voice.main"),
    ],
)
def test_voice_sdk_functions_exposed_through_litellm_namespace(
    name: str, expected_module: str
) -> None:
    fn = getattr(litellm, name)
    assert callable(fn)
    assert fn.__module__ == expected_module


def test_voice_slice_package_reexports() -> None:
    from litellm.endpoints.voice import acreate_voice, ascript, create_voice, script

    assert all(callable(f) for f in (acreate_voice, ascript, create_voice, script))


def test_create_voice_signature_keeps_required_params() -> None:
    import inspect

    sig = inspect.signature(litellm.create_voice)
    params = list(sig.parameters)
    # The two required positional params must remain first for router delegation.
    assert params[:2] == ["model", "voice_data"]
    assert "voice_data" in sig.parameters
    assert "model" in sig.parameters

def test_create_voice_forwards_caller_extra_headers(monkeypatch) -> None:
    """A caller-supplied extra_headers must reach the HTTP handler.

    The slice used to read ``extra_headers`` back out of ``**kwargs`` and assign
    it over the named parameter. Python routes an explicit ``extra_headers=``
    into the parameter, never into ``kwargs``, so that assignment always wrote
    None and the caller's headers were silently dropped.

    Calls the undecorated function: the ``@client`` wrapper only adds logging
    bookkeeping and needs a fully wired Logging object to run.
    """
    import litellm.endpoints.voice.main as voice_main
    from litellm.llms.custom_httpx.llm_http_handler import BaseLLMHTTPHandler

    captured: dict = {}

    def _fake_voice_handler(self, **kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(BaseLLMHTTPHandler, "voice_handler", _fake_voice_handler)

    voice_main.create_voice.__wrapped__(  # pyright: ignore[reportFunctionMemberAccess]  # @wraps exposes __wrapped__
        model="omnivoice/omnivoice",
        voice_data={"action": "register"},
        extra_headers={"x-caller": "kept"},
        litellm_logging_obj=_StubLoggingObj(),
    )

    assert captured["extra_headers"] == {"x-caller": "kept"}


class _StubLoggingObj:
    def update_environment_variables(self, **kwargs) -> None:
        return None


def test_acreate_voice_closes_logging_lifecycle(monkeypatch) -> None:
    """The voice handler must fire the terminal logging events.

    ``log_pre_api_call`` runs at pre-call and pairs with the terminal
    success/failure events in every other endpoint family. Voice calls never
    dispatched them, so pre-call pair-state in callbacks (the KEDA
    concurrent-requests gauge, budget guardrails) leaked permanently: the dev
    gateway's gauge stuck at 2.0 after two GET /v1/voices calls and never
    decayed.
    """

    import asyncio
    import httpx
    from datetime import datetime

    import litellm.llms.custom_httpx.llm_http_handler as handler_mod
    from litellm.litellm_core_utils.litellm_logging import Logging

    events: list[str] = []

    def _fake_pre_call(self, input, api_key, model=None, additional_args=None):
        events.append("pre")

    async def _fake_dispatch_success(self, result=None, start_time=None, **kwargs):
        events.append("success")

    async def _fake_dispatch_failure(self, exception, traceback_exception, **kwargs):
        events.append("failure")

    monkeypatch.setattr(Logging, "pre_call", _fake_pre_call)
    monkeypatch.setattr(Logging, "dispatch_success_handlers", _fake_dispatch_success)
    monkeypatch.setattr(Logging, "dispatch_failure_handlers", _fake_dispatch_failure)

    async def _fake_send(**kwargs):
        return httpx.Response(
            status_code=200,
            json={"voices": [{"id": "alloy"}]},
            request=httpx.Request("GET", "http://omnivoice/v1/voices"),
        )

    monkeypatch.setattr(handler_mod, "_send_voice_request_async", _fake_send)

    async def main() -> dict:
        return await handler_mod.BaseLLMHTTPHandler().voice_handler(
            model="omnivoice/omnivoice",
            voice_data={"action": "list"},
            voice_provider_config=litellm.ProviderConfigManager.get_provider_voice_config(
                provider=litellm.LlmProviders.OMNIVOICE,
            ),
            optional_params={},
            litellm_params={
                "model": "omnivoice/omnivoice",
                "custom_llm_provider": "omnivoice",
                "api_base": "http://omnivoice:50051/v1",
                "metadata": {},
                "litellm_call_id": "test-lifecycle",
            },
            logging_obj=Logging(
                model="omnivoice/omnivoice",
                messages=[],
                stream=False,
                call_type="acreate_voice",
                start_time=datetime.now(),
                litellm_call_id="test-lifecycle",
                function_id="test-lifecycle",
            ),
            timeout=30.0,
            _is_async=True,
        )

    result = asyncio.run(main())
    assert result["voices"][0]["id"] == "alloy"
    assert events == ["pre", "success"], (
        f"voice call did not close its logging lifecycle: {events}"
    )


def test_acreate_voice_dispatches_failure_event(monkeypatch) -> None:
    """A failing voice request must dispatch the failure event, not leak it."""

    import asyncio
    import httpx
    from datetime import datetime

    import litellm.llms.custom_httpx.llm_http_handler as handler_mod
    from litellm.litellm_core_utils.litellm_logging import Logging

    events: list[str] = []

    def _fake_pre_call(self, input, api_key, model=None, additional_args=None):
        events.append("pre")

    async def _fake_dispatch_success(self, result=None, start_time=None, **kwargs):
        events.append("success")

    async def _fake_dispatch_failure(self, exception, traceback_exception, **kwargs):
        events.append("failure")

    monkeypatch.setattr(Logging, "pre_call", _fake_pre_call)
    monkeypatch.setattr(Logging, "dispatch_success_handlers", _fake_dispatch_success)
    monkeypatch.setattr(Logging, "dispatch_failure_handlers", _fake_dispatch_failure)

    async def _fake_send(**kwargs):
        raise httpx.ConnectError("connection refused by voice pod")

    monkeypatch.setattr(handler_mod, "_send_voice_request_async", _fake_send)

    async def main() -> dict:
        return await handler_mod.BaseLLMHTTPHandler().voice_handler(
            model="omnivoice/omnivoice",
            voice_data={"action": "get_profile", "profile_id": "ghost"},
            voice_provider_config=litellm.ProviderConfigManager.get_provider_voice_config(
                provider=litellm.LlmProviders.OMNIVOICE,
            ),
            optional_params={},
            litellm_params={
                "model": "omnivoice/omnivoice",
                "custom_llm_provider": "omnivoice",
                "api_base": "http://omnivoice:50051/v1",
                "metadata": {},
                "litellm_call_id": "test-lifecycle",
            },
            logging_obj=Logging(
                model="omnivoice/omnivoice",
                messages=[],
                stream=False,
                call_type="acreate_voice",
                start_time=datetime.now(),
                litellm_call_id="test-lifecycle",
                function_id="test-lifecycle",
            ),
            timeout=30.0,
            _is_async=True,
        )

    try:
        asyncio.run(main())
        raise AssertionError("a 404 voice response must raise through the handler")
    except AssertionError:
        raise
    except Exception:
        assert events == ["pre", "failure"], (
            f"a failed voice call must dispatch the failure event: {events}"
        )


def test_acreate_voice_classified_as_async_request() -> None:
    """Voice calls must take the async dispatch branch of the logging system.

    ``is_sync_litellm_request`` whitelists the async call-type kwargs; voice
    was missing from it, so a proxy voice request classified as sync SDK and
    ``dispatch_success_handlers`` ran only the sync ``success_handler``. Async
    callbacks (the KEDA gauge, budget guardrails) never saw the terminal event
    and the concurrent-requests gauge leaked one inc per voice request.
    """

    from litellm.litellm_core_utils.litellm_logging import Logging

    assert Logging.is_sync_litellm_request({"acreate_voice": True}) is False
    assert Logging.is_sync_litellm_request({}) is True


def test_acreate_voice_stamps_async_flag_into_logging_snapshot(monkeypatch) -> None:
    """The flag must actually reach the logging snapshot, not just the call kwargs.

    ``acreate_voice`` is a named parameter of ``create_voice``, so it never
    flows through ``**call_kwargs`` into the ``update_environment_variables``
    snapshot: it must be spelled out in the literal. Without it the whitelist
    entry is dead code and the request still classifies as sync SDK.
    """

    import asyncio
    from datetime import datetime

    import httpx

    import litellm.endpoints.voice.main as voice_main
    import litellm.llms.custom_httpx.llm_http_handler as handler_mod
    from litellm.litellm_core_utils.litellm_logging import Logging

    captured: dict = {}

    class _TrackLogging(Logging):
        async def dispatch_success_handlers(self, result=None, start_time=None, **kwargs):
            snapshot: Final = self.model_call_details.get("litellm_params", {})
            captured["flag"] = snapshot.get("acreate_voice")
            captured["classified_sync"] = self.is_sync_litellm_request(snapshot)
            captured["dispatched"] = True
            return None

    async def _fake_send(**kwargs):
        return httpx.Response(
            status_code=200,
            json={"voices": []},
            request=httpx.Request("GET", "http://omnivoice/v1/voices"),
        )

    monkeypatch.setattr(handler_mod, "_send_voice_request_async", _fake_send)

    async def main() -> dict:
        return await voice_main.create_voice.__wrapped__(
            model="omnivoice/omnivoice",
            voice_data={"action": "list"},
            api_base="http://omnivoice:50051/v1",
            acreate_voice=True,
            litellm_logging_obj=_TrackLogging(
                model="omnivoice/omnivoice",
                messages=[],
                stream=False,
                call_type="acreate_voice",
                start_time=datetime.now(),
                litellm_call_id="test-snapshot",
                function_id="test-snapshot",
            ),
        )

    result = asyncio.run(main())
    assert result == {"voices": []}
    assert captured["dispatched"] is True
    assert captured["flag"] is True, "acreate_voice flag never reached the logging snapshot"
    assert captured["classified_sync"] is False
