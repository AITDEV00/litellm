import httpx
import pytest

from litellm.litellm_core_utils.llm_request_utils import (
    ensure_extra_body_is_safe,
    flatten_form_field_values,
    serialize_multipart_form_fields,
)


def _multipart_field_names(data: dict) -> list[str]:
    request = httpx.Request(
        "POST",
        "http://backend/v1/images/edits",
        data=data,
        files=[("image[]", ("in.png", b"stub", "image/png"))],
    )
    request.read()
    body = request.content.decode("utf-8", "replace")
    prefix = 'Content-Disposition: form-data; name="'
    return [line[len(prefix) : line.index('"', len(prefix))] for line in body.splitlines() if line.startswith(prefix)]


def test_serialize_multipart_form_fields_flattens_like_the_openai_sdk():
    fields = serialize_multipart_form_fields(
        {
            "model": "sora-2",
            "prompt": "a cat surfing",
            "hd": True,
            "watermark": False,
            "seconds": 4,
            "size": None,
            "metadata": {"trace": {"id": "t1"}},
            "characters": [{"id": "char_1", "name": "Mia"}, "solo"],
        }
    )

    assert fields == (
        ("model", (None, "sora-2")),
        ("prompt", (None, "a cat surfing")),
        ("hd", (None, "true")),
        ("watermark", (None, "false")),
        ("seconds", (None, "4")),
        ("metadata[trace][id]", (None, "t1")),
        ("characters[][id]", (None, "char_1")),
        ("characters[][name]", (None, "Mia")),
        ("characters[]", (None, "solo")),
    )


def test_serialize_multipart_form_fields_drops_empty_strings():
    assert serialize_multipart_form_fields({"prompt": "", "model": "sora-2"}) == (("model", (None, "sora-2")),)


def test_serialize_multipart_form_fields_empty_body():
    assert serialize_multipart_form_fields({}) == ()


def test_flatten_form_field_values_flattens_nested_and_drops_empty():
    assert flatten_form_field_values(
        {
            "seed": 42,
            "hd": True,
            "size": None,
            "prompt": "",
            "generation_config": {"steps": 30, "guidance": True},
        }
    ) == (
        ("seed", "42"),
        ("hd", "true"),
        ("generation_config[steps]", "30"),
        ("generation_config[guidance]", "true"),
    )


def test_flatten_form_field_values_later_source_wins_on_collision():
    assert flatten_form_field_values({"seed": 1}, None, {"seed": 2}) == (
        ("seed", "1"),
        ("seed", "2"),
    )
    assert dict(flatten_form_field_values({"seed": 1}, {"seed": 2}))["seed"] == "2"


def test_flatten_form_field_values_keeps_scalar_lists_as_repeated_fields():
    assert flatten_form_field_values({"loras": ["a", "b", "c"], "generation_config": {"tags": [1, 2]}, "seed": 42}) == (
        ("loras", ("a", "b", "c")),
        ("generation_config[tags]", ("1", "2")),
        ("seed", "42"),
    )


def test_flatten_form_field_values_scalar_list_survives_update_into_multipart():
    request_params: dict = {"model": "my-edit-model"}
    request_params.update(flatten_form_field_values({"loras": ["style_a", "style_b"]}))

    names = _multipart_field_names(request_params)

    assert names.count("loras") == 2
    assert names.count("model") == 1


def test_flatten_form_field_values_rejects_over_deep_nesting():
    nested: object = "leaf"
    for _ in range(102):
        nested = {"k": nested}
    assert isinstance(nested, dict)
    with pytest.raises(ValueError, match="max depth"):
        flatten_form_field_values(nested)


def test_ensure_extra_body_is_safe_converts_prompt_without_filtering_metadata_keys():
    from typing import cast

    class Prompt:
        pass

    prompt = Prompt()
    metadata: dict[object, object] = {"prompt": prompt, 1: "retained"}
    extra_body = cast(dict[str, object], {"metadata": metadata})

    result = ensure_extra_body_is_safe(extra_body)

    assert result is extra_body
    assert metadata == {"prompt": prompt.__dict__, 1: "retained"}


def test_ensure_extra_body_is_safe_returns_non_dict_unchanged():
    from collections import UserDict
    from typing import cast

    extra_body = UserDict({"metadata": {"prompt": object()}})

    result = ensure_extra_body_is_safe(cast(dict[str, object], extra_body))

    assert result is extra_body


# ---------------------------------------------------------------------------
# native_multipart_fields: the typed contract for native multipart audio
# ---------------------------------------------------------------------------


def _encode_multipart(
    fields: tuple[tuple[str, str | int | float | bool], ...],
) -> dict[str, list[str]]:
    """Encode the fields the way the transport sends them (httpx ``data=`` is a
    mapping: the request builder collapses the ordered pairs into an
    insertion-ordered dict whose list values repeat a field) and parse the
    wire body back with a form-conforming parser, so the test observes the
    actual protocol rather than the intermediate pair list."""
    data: dict[str, object] = {}
    for name, value in fields:
        existing = data.get(name)
        if isinstance(existing, list):
            existing.append(value)
        elif existing is not None:
            data[name] = [existing, value]
        else:
            data[name] = value

    request = httpx.Request(
        "POST",
        "http://backend/v1/audio/transcriptions",
        data=data,
        files=[("file", ("a.wav", b"RIFF....", "audio/wav"))],
    )
    request.read()
    body = request.content.decode("utf-8", "replace")
    boundary = body.split("\r\n")[0].strip()
    parsed: dict[str, list[str]] = {}
    for section in body.split(boundary)[1:]:
        if 'name="' not in section:
            continue
        header_block, _, value = section.partition("\r\n\r\n")
        disposition_line = next(line for line in header_block.splitlines() if "form-data" in line)
        name_start: Final = disposition_line.index('name="') + len('name="')
        name = disposition_line[name_start:].split('"')[0]
        if "filename=" in disposition_line:
            continue
        parsed.setdefault(name, []).append(value[: value.rfind("\r\n")])
    return parsed


class TestNativeMultipartFields:
    def test_preserves_order_and_scalar_types(self):
        from litellm.litellm_core_utils.llm_request_utils import native_multipart_fields

        fields = native_multipart_fields(
            {"model": "nemotron"},
            {"language": "en", "temperature": 0.0, "diarization": True},
        )
        assert fields == (
            ("model", "nemotron"),
            ("language", "en"),
            ("temperature", 0.0),
            ("diarization", True),
        )

    def test_repeats_list_values_in_order(self):
        from litellm.litellm_core_utils.llm_request_utils import native_multipart_fields

        fields = native_multipart_fields({"timestamp_granularities": ["word", "segment"]})
        assert fields == (
            ("timestamp_granularities", "word"),
            ("timestamp_granularities", "segment"),
        )

    def test_extra_body_unpacks_top_level(self):
        from litellm.litellm_core_utils.llm_request_utils import native_multipart_fields

        fields = native_multipart_fields({"model": "m"}, extra_body={"custom_k": "v"})
        assert ("custom_k", "v") in fields
        assert all(name != "extra_body" for name, _ in fields)

    def test_extra_body_cannot_override_model(self):
        from litellm.litellm_core_utils.llm_request_utils import native_multipart_fields

        fields = native_multipart_fields({"model": "resolved-model"}, extra_body={"model": "attacker"})
        assert ("model", "resolved-model") in fields
        assert fields.count(("model", "attacker")) == 0

    def test_filters_litellm_owned_kwargs(self):
        from litellm.litellm_core_utils.llm_request_utils import native_multipart_fields

        fields = native_multipart_fields(
            {
                "keepalive_seconds": 3600,
                "allow_client_keepalive_override": True,
                "litellm_call_id": "abc",
                "metadata": {"x": 1},
            }
        )
        assert fields == ()

    def test_drops_non_scalar_shapes(self):
        from litellm.litellm_core_utils.llm_request_utils import native_multipart_fields

        fields = native_multipart_fields({"nested": {"a": 1}, "uploads": [b"bytes"]})
        assert fields == ()

    def test_first_source_wins_on_collision(self):
        from litellm.litellm_core_utils.llm_request_utils import native_multipart_fields

        fields = native_multipart_fields({"a": 1}, {"a": 2})
        assert fields == (("a", 1),)

    def test_wire_body_repeats_fields_and_spells_bools(self):
        """End-to-end through the actual httpx multipart encoder.

        Repeated parts survive as two parts in the wire body, and a boolean is
        spelled lowercase, matching what native servers parse from form data.
        """
        from litellm.litellm_core_utils.llm_request_utils import native_multipart_fields

        fields = native_multipart_fields(
            {"model": "m", "diarization": True, "timestamp_granularities": ["word", "segment"]},
            extra_body={"word_timestamps": True},
        )
        parsed = _encode_multipart(fields)
        assert parsed["model"] == ["m"]
        assert parsed["diarization"] == ["true"]
        assert parsed["word_timestamps"] == ["true"]
        assert parsed["timestamp_granularities"] == ["word", "segment"]
