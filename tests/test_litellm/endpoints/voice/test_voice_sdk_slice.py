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
