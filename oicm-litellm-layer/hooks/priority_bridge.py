"""
Priority Bridge — injects server-side priority fields from HTB priority.

This is the bridge between the HTB priority rate limiter (proxy layer, string
priorities like "prior1") and server-side priority preemption (GPU layer,
integer priorities like 0). It reads the `htb_priority` ContextVar set by
DynamicRateLimitHandlerV3 and looks up a config-driven map
(`priority_body_fields` in litellm_settings) to inject field-value pairs
into `data["extra_body"]`.

Register in config.yaml:

    litellm_settings:
      priority_body_fields:
        prior1:
          priority: 0
        prior2:
          priority: 100
        prior3:
          priority: 200
      callbacks:
        - litellm_hooks.priority_bridge.priority_bridge

See PRIORITY-BRIDGE-FEASIBILITY.md for the full end-to-end trace.
"""

import logging
from typing import Optional, Union

import litellm
from litellm.integrations.custom_logger import CustomLogger
from litellm.caching import DualCache
from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.hooks.dynamic_rate_limiter_v3 import htb_priority
from litellm.types.utils import CallTypesLiteral

logger = logging.getLogger("oicm-priority-bridge")

_CHAT_CALL_TYPES: frozenset[str] = frozenset({"completion", "acompletion"})


class PriorityBridge(CustomLogger):

    async def async_pre_call_hook(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        cache: DualCache,
        data: dict,
        call_type: CallTypesLiteral,
    ) -> Optional[Union[Exception, str, dict]]:
        body_fields_map = litellm.priority_body_fields
        if not body_fields_map:
            return None

        if call_type not in _CHAT_CALL_TYPES:
            return None

        htb_prio = htb_priority.get()
        if htb_prio is None:
            return None

        body_fields = body_fields_map.get(htb_prio)
        if not body_fields:
            return None

        extra_body = data.get("extra_body") or {}
        extra_body.update(body_fields)
        data["extra_body"] = extra_body

        logger.debug(
            "Injected priority body fields for htb_priority=%s: %s",
            htb_prio,
            list(body_fields.keys()),
        )

        return data


priority_bridge = PriorityBridge()
