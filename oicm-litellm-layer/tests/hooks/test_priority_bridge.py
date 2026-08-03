import pytest

import litellm
from litellm.proxy.hooks.dynamic_rate_limiter_v3 import htb_priority

from litellm_hooks.priority_bridge import PriorityBridge


@pytest.fixture
def hook():
    return PriorityBridge()


@pytest.fixture
def base_data():
    return {"model": "hosted_vllm/glm-5.2", "messages": [{"role": "user", "content": "hi"}]}


@pytest.fixture
def config_map():
    return {
        "prior1": {"priority": 0},
        "prior2": {"priority": 100},
        "prior3": {"priority": 200},
    }


@pytest.fixture(autouse=True)
def reset_state():
    yield
    htb_priority.set(None)
    litellm.priority_body_fields = None


class TestPriorityBridgeInjects:
    @pytest.mark.asyncio
    async def test_injects_priority_for_prior1(self, hook, base_data, config_map):
        litellm.priority_body_fields = config_map
        htb_priority.set("prior1")

        result = await hook.async_pre_call_hook(
            user_api_key_dict=None, cache=None, data=base_data, call_type="acompletion"
        )

        assert result is not None
        assert result["extra_body"]["priority"] == 0

    @pytest.mark.asyncio
    async def test_injects_priority_for_prior3(self, hook, base_data, config_map):
        litellm.priority_body_fields = config_map
        htb_priority.set("prior3")

        result = await hook.async_pre_call_hook(
            user_api_key_dict=None, cache=None, data=base_data, call_type="acompletion"
        )

        assert result is not None
        assert result["extra_body"]["priority"] == 200

    @pytest.mark.asyncio
    async def test_preserves_existing_extra_body(self, hook, config_map):
        litellm.priority_body_fields = config_map
        htb_priority.set("prior1")
        data = {
            "model": "hosted_vllm/glm-5.2",
            "messages": [],
            "extra_body": {"guided_json": {"type": "object"}},
        }

        result = await hook.async_pre_call_hook(
            user_api_key_dict=None, cache=None, data=data, call_type="acompletion"
        )

        assert result["extra_body"]["guided_json"] == {"type": "object"}
        assert result["extra_body"]["priority"] == 0

    @pytest.mark.asyncio
    async def test_injects_multiple_fields(self, hook, base_data):
        litellm.priority_body_fields = {
            "prior1": {"priority": 0, "urgency": "high"},
        }
        htb_priority.set("prior1")

        result = await hook.async_pre_call_hook(
            user_api_key_dict=None, cache=None, data=base_data, call_type="acompletion"
        )

        assert result["extra_body"] == {"priority": 0, "urgency": "high"}

    @pytest.mark.asyncio
    async def test_completion_call_type_also_works(self, hook, base_data, config_map):
        litellm.priority_body_fields = config_map
        htb_priority.set("prior2")

        result = await hook.async_pre_call_hook(
            user_api_key_dict=None, cache=None, data=base_data, call_type="completion"
        )

        assert result is not None
        assert result["extra_body"]["priority"] == 100


class TestPriorityBridgeNoOp:
    @pytest.mark.asyncio
    async def test_returns_none_when_no_config(self, hook, base_data):
        litellm.priority_body_fields = None
        htb_priority.set("prior1")

        result = await hook.async_pre_call_hook(
            user_api_key_dict=None, cache=None, data=base_data, call_type="acompletion"
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_empty_config(self, hook, base_data):
        litellm.priority_body_fields = {}
        htb_priority.set("prior1")

        result = await hook.async_pre_call_hook(
            user_api_key_dict=None, cache=None, data=base_data, call_type="acompletion"
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_htb_priority(self, hook, base_data, config_map):
        litellm.priority_body_fields = config_map
        htb_priority.set(None)

        result = await hook.async_pre_call_hook(
            user_api_key_dict=None, cache=None, data=base_data, call_type="acompletion"
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_priority_not_in_map(self, hook, base_data, config_map):
        litellm.priority_body_fields = config_map
        htb_priority.set("unknown_priority")

        result = await hook.async_pre_call_hook(
            user_api_key_dict=None, cache=None, data=base_data, call_type="acompletion"
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_for_non_chat_call_type(self, hook, base_data, config_map):
        litellm.priority_body_fields = config_map
        htb_priority.set("prior1")

        result = await hook.async_pre_call_hook(
            user_api_key_dict=None, cache=None, data=base_data, call_type="aembedding"
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_does_not_mutate_data_when_noop(self, hook, base_data, config_map):
        litellm.priority_body_fields = config_map
        htb_priority.set(None)

        result = await hook.async_pre_call_hook(
            user_api_key_dict=None, cache=None, data=base_data, call_type="acompletion"
        )
        assert result is None
        assert "extra_body" not in base_data


class TestPriorityBridgeAliasManyToMany:
    @pytest.mark.asyncio
    async def test_alias_maps_to_same_fields(self, hook, base_data):
        litellm.priority_body_fields = {
            "prior1": {"priority": 0, "urgency": "high"},
            "urgent": {"priority": 0, "urgency": "high"},
        }
        htb_priority.set("urgent")

        result = await hook.async_pre_call_hook(
            user_api_key_dict=None, cache=None, data=base_data, call_type="acompletion"
        )

        assert result["extra_body"] == {"priority": 0, "urgency": "high"}

    @pytest.mark.asyncio
    async def test_empty_fields_dict_is_noop(self, hook, base_data):
        litellm.priority_body_fields = {"prior1": {}}
        htb_priority.set("prior1")

        result = await hook.async_pre_call_hook(
            user_api_key_dict=None, cache=None, data=base_data, call_type="acompletion"
        )
        assert result is None
