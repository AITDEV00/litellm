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