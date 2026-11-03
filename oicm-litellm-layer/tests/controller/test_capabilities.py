"""Tests for the capability metadata stamp (controller/capabilities.py)."""

from controller.capabilities import (
    CapabilityFacts,
    capability_metadata,
    facts_from_sglang_model_info,
    probe_verdict_to_fact,
)


class TestFactsFromSglangModelInfo:
    def test_bool_flags_are_read(self):
        facts = facts_from_sglang_model_info(
            {
                "has_image_understanding": True,
                "has_audio_understanding": False,
                "is_generation": True,
            }
        )
        assert facts == CapabilityFacts(accepts_image=True, accepts_audio=False)

    def test_missing_or_nonbool_flags_read_as_unknown_not_false(self):
        facts = facts_from_sglang_model_info({"has_image_understanding": "yes"})
        assert facts == CapabilityFacts(accepts_image=None, accepts_audio=None)

    def test_non_dict_payload_is_all_unknown(self):
        assert facts_from_sglang_model_info(None) == CapabilityFacts()

    def test_ti2i_task_types_suppress_all_claims(self):
        """A TI2I server's image flag describes image editing, not
        understanding; its chat endpoint generates images, so stamping
        supports_vision would route understanding-style image chats to a
        diffusion server. Its own served task types say so."""
        facts = facts_from_sglang_model_info(
            {"has_image_understanding": True, "is_generation": True},
            served_task_types=frozenset({"TI2I"}),
        )
        assert facts == CapabilityFacts()

    def test_mixed_task_types_keep_the_stamp(self):
        """A server that also serves chat-style generation is a real chat
        surface, so its flags stand."""
        facts = facts_from_sglang_model_info(
            {"has_image_understanding": True},
            served_task_types=frozenset({"TI2I", "chat"}),
        )
        assert facts.accepts_image is True

    def test_empty_task_types_do_not_suppress(self):
        """A server that does not self-declare task types is judged by its
        flags alone; an empty declaration must not read as TI2I-only."""
        facts = facts_from_sglang_model_info(
            {"has_image_understanding": False}, served_task_types=frozenset()
        )
        assert facts.accepts_image is False


class TestProbeVerdictToFact:
    def test_200_is_acceptance(self):
        assert probe_verdict_to_fact(200) is True

    def test_400_is_rejection(self):
        assert probe_verdict_to_fact(400) is False

    def test_422_is_rejection(self):
        assert probe_verdict_to_fact(422) is False

    def test_500_is_unknown_not_rejection(self):
        """A server error says nothing about the capability; claiming
        rejection would permanently hide a working one."""
        assert probe_verdict_to_fact(500) is None

    def test_503_is_unknown(self):
        assert probe_verdict_to_fact(503) is None


class TestCapabilityMetadata:
    def test_vision_true_stamps_the_flag(self):
        stamp = capability_metadata(
            "chat", CapabilityFacts(accepts_image=True, accepts_audio=False)
        )
        assert stamp == {"supports_vision": True, "supports_audio_input": False}

    def test_text_only_model_stamps_explicit_false(self):
        """A definitive negative is stamped, not omitted: clients treat null
        as false anyway, and the explicit value documents the probe verdict."""
        stamp = capability_metadata(
            "chat", CapabilityFacts(accepts_image=False, accepts_audio=False)
        )
        assert stamp == {"supports_vision": False, "supports_audio_input": False}

    def test_unknown_facts_claim_nothing(self):
        assert capability_metadata("chat", None) == {}
        assert (
            capability_metadata("chat", CapabilityFacts()) == {}
        )

    def test_audio_true_stamps_the_audio_flag(self):
        stamp = capability_metadata(
            "chat", CapabilityFacts(accepts_image=False, accepts_audio=True)
        )
        assert stamp == {"supports_vision": False, "supports_audio_input": True}

    def test_non_chat_mode_claims_nothing(self):
        """A transcription or TTS row is not a chat surface; its clients do
        not read these keys and the stamp would be noise."""
        assert (
            capability_metadata(
                "audio_transcription", CapabilityFacts(accepts_image=True)
            )
            == {}
        )

    def test_no_architecture_block_is_stamped(self):
        """The litellm extension derives modalities client-side from the
        supports_ flags and never reads model_info.architecture, so the
        server-side block would be dead weight."""
        stamp = capability_metadata(
            "chat", CapabilityFacts(accepts_image=True, accepts_audio=True)
        )
        assert "architecture" not in stamp
