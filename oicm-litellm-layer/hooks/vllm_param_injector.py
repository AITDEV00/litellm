"""
VLLM Param Injector — relocates vLLM-specific params into extra_body.

This is Component #3 of the OICM→LiteLLM integration layer.
It plugs into LiteLLM via `litellm_settings.callbacks` in config.yaml.

How it works:
1. Client sends a request with vLLM-specific params (guided_json, thinking_token_budget, etc.)
2. LiteLLM's hosted_vllm provider doesn't recognize these → would drop or error
3. This hook intercepts the request BEFORE it's sent to the provider
4. It relocates vLLM-specific params from data → data["extra_body"]
5. The openai_like handler then merges extra_body into the HTTP request body

Why this works without forking:
- The openai_like chat handler (handler.py:278) does: data = {**optional_params, **extra_body}
- extra_body is spread into the final HTTP body, bypassing param validation
- This hook runs before param validation, so we can move params into extra_body early

Supported vLLM-specific params (relocated to extra_body):
  - guided_json: JSON schema for structured output
  - guided_regex: Regex for structured output
  - guided_choice: List of choices for structured output
  - guided_grammar: Lark grammar for structured output
  - guided_decoding_backend: Backend for guided decoding
  - guided_whitespace_pattern: Pattern for whitespace in guided decoding
  - thinking_token_budget: Integer budget for thinking tokens (vLLM native)
  - reasoning_parser: Parser for reasoning content
  - chat_template: Custom chat template
  - add_reasoning: Enable reasoning output
  - lora_name: LoRA adapter name

For embedding requests, this hook currently cannot help because the embedding
handler doesn't merge extra_body. See the embedding patch in /patches/.
"""

import copy
import logging
from typing import Any, Dict, Optional, Union

from litellm.integrations.custom_logger import CustomLogger
from litellm.proxy._types import UserAPIKeyAuth
from litellm.caching import DualCache
from litellm.types.utils import CallTypesLiteral

logger = logging.getLogger("oicm-vllm-injector")

# ─── vLLM-specific params that should be relocated to extra_body ───────────

VLLM_CHAT_PARAMS = {
    # Guided decoding
    "guided_json",
    "guided_regex",
    "guided_choice",
    "guided_grammar",
    "guided_decoding_backend",
    "guided_whitespace_pattern",
    # Thinking/reasoning
    "thinking_token_budget",
    "reasoning_parser",
    "add_reasoning",
    # Chat template
    "chat_template",
    # LoRA
    "lora_name",
    # Other vLLM params
    "prefix_cache_n",
    "kv_cache_dtype",
    "num_lookup_tokens",
}

# Params that are already handled by LiteLLM's hosted_vllm provider
# (don't relocate these — they're already mapped correctly)
ALREADY_HANDLED = {
    "reasoning_effort",  # Already in get_supported_openai_params()
    "thinking",          # Already handled (but lossy — see note below)
    "temperature",
    "top_p",
    "max_tokens",
    "max_completion_tokens",
    "stream",
    "stop",
    "frequency_penalty",
    "presence_penalty",
    "seed",
    "response_format",
    "tools",
    "tool_choice",
    "logprobs",
    "top_logprobs",
}


class VllmParamInjector(CustomLogger):
    """
    Custom callback that relocates vLLM-specific params into extra_body.
    
    Register in config.yaml:
    
        litellm_settings:
          callbacks: hooks.vllm_param_injector.VllmParamInjector
    """

    async def async_pre_call_hook(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        cache: DualCache,
        data: dict,
        call_type: CallTypesLiteral,
    ) -> Optional[Union[Exception, str, dict]]:
        """
        Pre-call hook that runs before the request is sent to the provider.
        
        For hosted_vllm models, relocate vLLM-specific params into extra_body
        so they bypass LiteLLM's param validation and get passed through to vLLM.
        """
        model = data.get("model", "")
        
        # Only process hosted_vllm models
        if not model.startswith("hosted_vllm/"):
            return None
        
        # Only process chat completion calls
        if call_type not in ("completion", "acompletion"):
            return None
        
        # Find vLLM-specific params that need relocation
        params_to_relocate = {}
        for param in VLLM_CHAT_PARAMS:
            if param in data:
                params_to_relocate[param] = data.pop(param)
        
        if not params_to_relocate:
            return None
        
        # Merge into extra_body
        extra_body = data.get("extra_body", {})
        if extra_body is None:
            extra_body = {}
        extra_body.update(params_to_relocate)
        data["extra_body"] = extra_body
        
        logger.info(
            f"Relocated vLLM params to extra_body for {model}: "
            f"{list(params_to_relocate.keys())}"
        )
        
        # Return the modified data dict
        return data


class ThinkingTokenBudgetFixer(CustomLogger):
    """
    Optional: Fixes the lossy thinking→reasoning_effort conversion.
    
    LiteLLM's hosted_vllm handler converts thinking.budget_tokens into
    coarse reasoning_effort levels (high/medium/low/minimal), losing precision.
    
    If the client sends `thinking_token_budget` as a top-level param,
    this hook relocates it to extra_body BEFORE the hosted_vllm handler
    processes it, ensuring the integer value reaches vLLM directly.
    
    Note: This is a separate class so you can enable it independently.
    For most setups, VllmParamInjector already handles this.
    """

    async def async_pre_call_hook(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        cache: DualCache,
        data: dict,
        call_type: CallTypesLiteral,
    ) -> Optional[Union[Exception, str, dict]]:
        """Relocate thinking_token_budget to extra_body for integer precision."""
        model = data.get("model", "")
        if not model.startswith("hosted_vllm/"):
            return None
        
        if "thinking_token_budget" not in data:
            return None
        
        budget = data.pop("thinking_token_budget")
        extra_body = data.get("extra_body", {})
        if extra_body is None:
            extra_body = {}
        extra_body["thinking_token_budget"] = budget
        data["extra_body"] = extra_body
        
        logger.info(f"Relocated thinking_token_budget={budget} to extra_body for {model}")
        return data
