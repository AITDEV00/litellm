"""
SessionIdentityResolver: infer a stable session id when the client sends none.

A CustomLogger pre-call callback (litellm_settings.callbacks), so it runs in
ProxyLogging.pre_call_hook (litellm/proxy/utils.py) BEFORE route_request ->
Router.acompletion -> DeploymentAffinityCheck. Whatever it stamps into
``metadata["session_id"]`` is what the existing session-affinity machinery
pins on. LiteLLM stays the only router; this class never selects deployments.

Precedence (highest first):
1. explicit ``metadata.session_id``          client-owned, pass through
2. declared ``prompt_cache_key`` / ``conversation`` body field, resolved to a
   deterministic id WITHOUT Redis (same name -> same id across pods)
3. history lineage                            recover a previously taught id
4. synthesized                                fresh id for a new conversation

A policy-generated ``session_id`` (``SESSION_ID_GENERATED_METADATA_KEY``) is
treated as absent: DeploymentAffinityCheck ignores ids carrying that marker,
so we preserve the generated value under ``litellm_session_id_policy_generated``,
REMOVE the marker, and stamp the inferred id in its place.

The inferred id is stamped on the FIRST request carrying no id, so the
existing affinity workflow applies unchanged: that first request routes
normally and the selected deployment becomes the pin; the next request of the
same conversation (recovered via history or the same declared name) hits the
pin.

Scope, model group, matched depth, and source are resolved once pre-call and
stamped into the request metadata under ``_session_identity_*`` keys; the
success callback reads them verbatim rather than recomputing (avoids the
double-hashing class of bug).
"""

import logging
from typing import Any, Final

from litellm.constants import SESSION_ID_GENERATED_METADATA_KEY
from litellm.integrations.custom_logger import CustomLogger
from litellm.litellm_core_utils.core_helpers import get_or_create_metadata_bucket
from litellm.router_utils.session_identity.config import SessionIdentityConfig
from litellm.router_utils.session_identity.lineage import (
    IdentityResolution,
    build_chain,
    declared_id,
    scoped_declared_session_id,
    synthesized_session_id,
)
from litellm.router_utils.session_identity.store import SessionIdentityStore

verbose_logger = logging.getLogger("litellm")

_SUPPORTED_CALL_TYPES: Final = frozenset({"completion", "acompletion"})

# internal per-request state, stamped pre-call and read verbatim post-call
_META_SCOPE: Final = "_session_identity_scope"
_META_MODEL_GROUP: Final = "_session_identity_model_group"
_META_DEPTH: Final = "_session_identity_match_depth"
_META_SOURCE: Final = "_session_identity_source"
_META_DECLARED: Final = "litellm_session_identity_declared"
_META_INFERRED: Final = "litellm_session_id_inferred"
_META_POLICY_GENERATED: Final = "litellm_session_id_policy_generated"


class SessionIdentityResolver(CustomLogger):
    def __init__(self, config: SessionIdentityConfig | None = None, cache: Any = None):
        self.config = config or SessionIdentityConfig()
        self._store: SessionIdentityStore | None = (
            SessionIdentityStore(cache=cache, ttl_seconds=self.config.ttl_seconds) if cache is not None else None
        )

    def _store_for(self, cache: Any) -> SessionIdentityStore | None:
        """Store bound to the cache the hook received (created lazily)."""
        if not self.config.enabled or cache is None:
            return None
        if self._store is None or self._store.cache is not cache:
            self._store = SessionIdentityStore(cache=cache, ttl_seconds=self.config.ttl_seconds)
        return self._store

    @staticmethod
    def _model_group(data: dict) -> str:
        model = data.get("model")
        return model if isinstance(model, str) and model else "unknown"

    @staticmethod
    def _caller_scope(user_api_key_dict: Any) -> str:
        """
        Stable caller scope for lineage keys.

        ``UserAPIKeyAuth.api_key`` is already the hashed token, and it is the
        same value ``DeploymentAffinityCheck`` scopes its pins with
        (``metadata.user_api_key_hash``), so it is used directly here.
        """
        api_key = getattr(user_api_key_dict, "api_key", None)
        return str(api_key) if api_key else "anonymous"

    async def _resolve(self, store: SessionIdentityStore, data: dict, model_group: str, scope: str) -> IdentityResolution | None:
        """
        Resolve-or-create the session id for a request. Declared ids resolve
        without a store round-trip; history ids via the lineage store; a fresh
        non-trivial conversation gets a synthesized id so its first taught
        lineage and the id used to pin it agree from turn one.
        """
        declared = declared_id(data)
        if declared is not None:
            return IdentityResolution(
                session_id=scoped_declared_session_id(declared, model_group, scope),
                matched_depth=0,
                source="declared",
                declared=declared,
            )

        chain = build_chain(data=data, model_group=model_group, cache_salt=self.config.cache_salt, chunk_size=self.config.chunk_size_bytes)
        if not chain:
            return None
        match = await store.lookup(chain=chain, model_group=model_group, scope=scope)
        if match is not None and match.is_continuation():
            return IdentityResolution(session_id=match.session_id, matched_depth=match.matched_depth, source="history")
        return IdentityResolution(
            session_id=synthesized_session_id(chain=chain, model_group=model_group, scope=scope),
            matched_depth=0,
            source="synthesized",
        )

    async def async_pre_call_hook(self, user_api_key_dict: Any, cache: Any, data: dict, call_type: str) -> dict:
        if call_type not in _SUPPORTED_CALL_TYPES:
            return data
        store = self._store_for(cache)
        if store is None:
            return data

        _bucket_name, metadata = get_or_create_metadata_bucket(data)
        existing = metadata.get("session_id")
        generated = bool(metadata.get(SESSION_ID_GENERATED_METADATA_KEY))
        if isinstance(existing, str) and existing and not generated:
            return data  # client-supplied: affinity already uses it
        if generated and existing:
            metadata[_META_POLICY_GENERATED] = existing
            metadata.pop("session_id", None)
            metadata.pop(SESSION_ID_GENERATED_METADATA_KEY, None)  # let the inferred id pin
        if isinstance(data.get("litellm_session_id"), str) and data.get("litellm_session_id"):
            return data  # session/vendor header id: already recognized

        model_group = self._model_group(data)
        scope = self._caller_scope(user_api_key_dict)
        try:
            resolution = await self._resolve(store=store, data=data, model_group=model_group, scope=scope)
        except Exception as e:
            verbose_logger.warning("session_identity: resolution failed, routing normally: %s", e)
            return data

        if resolution is None or not resolution.session_id:
            return data
        metadata["session_id"] = resolution.session_id
        metadata[_META_INFERRED] = True
        metadata[_META_SCOPE] = scope
        metadata[_META_MODEL_GROUP] = model_group
        metadata[_META_DEPTH] = resolution.matched_depth
        metadata[_META_SOURCE] = resolution.source
        if resolution.declared:
            metadata[_META_DECLARED] = resolution.declared
        verbose_logger.debug("session_identity: resolved session (source=%s, depth=%d)", resolution.source, resolution.matched_depth)
        return data

    async def async_log_success_event(self, kwargs: dict, response_obj: Any, start_time: Any, end_time: Any) -> None:
        store = self._store
        if store is None or not self.config.enabled:
            return
        metadata = kwargs.get("litellm_params", {}).get("metadata") or kwargs.get("metadata") or {}
        session_id = metadata.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return

        request_data = kwargs.get("litellm_params", {}).get("proxy_server_request", {}) or {}
        body = request_data.get("body") if isinstance(request_data, dict) else None
        data = body if isinstance(body, dict) else kwargs
        messages_source = data if isinstance(data.get("messages"), list) else kwargs
        if not isinstance(messages_source.get("messages"), list):
            return

        # Read the stamped request state verbatim; never recompute scope/depth.
        scope = metadata.get(_META_SCOPE) or metadata.get("user_api_key_hash") or "anonymous"
        model_group = metadata.get(_META_MODEL_GROUP) or self._model_group(messages_source)
        start_index = metadata.get(_META_DEPTH) if isinstance(metadata.get(_META_DEPTH), int) else 0
        try:
            chain = build_chain(
                data=messages_source,
                model_group=model_group,
                cache_salt=self.config.cache_salt,
                chunk_size=self.config.chunk_size_bytes,
            )
            if not chain:
                return
            await store.teach(chain=chain, session_id=session_id, model_group=model_group, scope=scope, start_index=start_index)
        except Exception as e:
            verbose_logger.warning("session_identity: teach failed: %s", e)
