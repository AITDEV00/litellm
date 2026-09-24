"""
SessionIdentityResolver: infer a stable session id when the client sends none.

A CustomLogger pre-call callback (litellm_settings.callbacks), so it runs in
ProxyLogging.pre_call_hook (litellm/proxy/utils.py) BEFORE route_request ->
Router.acompletion -> DeploymentAffinityCheck. Whatever it stamps into
``metadata["session_id"]`` is what the existing session-affinity machinery
pins on. LiteLLM stays the only router; this class never selects deployments.

Skip rules (in order):
- explicit ``metadata.session_id``: client-owned, pass through untouched
- ``SESSION_ID_GENERATED_METADATA_KEY``: apply_missing_session_id_policy
  already minted a per-request id; the affinity check ignores those and so
  do we (never remove or overwrite the marker)
- ``data["litellm_session_id"]``: a recognized session/vendor header was
  extracted by add_litellm_data_to_request; affinity already uses it
- call types we cannot frame (embeddings, images, ...): skip

Otherwise the id is either the client's declared conversation name
(``prompt_cache_key`` / ``conversation`` body field, llm-d alias.go) or
inferred from the hash-chain lineage, and is stamped with
``SESSION_ID_INFERRED_METADATA_KEY`` so downstream code can distinguish
inferred ids. The GENERATED marker is deliberately NOT set: the affinity
check skips ids carrying it.

Teaching (chain hash -> session id) happens in async_log_success_event, when
the serving deployment is known. The deployment pin itself is written by
DeploymentAffinityCheck on the NEXT request after resolving this id, so an
inferred conversation is unpinned for its first two turns and pinned from the
third.
"""

from typing import Any, Final

from litellm.integrations.custom_logger import CustomLogger
from litellm.litellm_core_utils.core_helpers import get_or_create_metadata_bucket
from litellm.router_utils.session_identity.config import SessionIdentityConfig
from litellm.router_utils.session_identity.hash_chain import declared_id
from litellm.router_utils.session_identity.history_matcher import HistoryMatcher
from litellm.router_utils.session_identity.store import SessionIdentityStore

_SUPPORTED_CALL_TYPES: Final = frozenset({"completion", "acompletion"})


class SessionIdentityResolver(CustomLogger):
    def __init__(
        self,
        config: SessionIdentityConfig | None = None,
        cache: Any = None,
    ):
        self.config = config or SessionIdentityConfig.from_env()
        # cache is the DualCache the proxy hands the hook; kept settable so
        # the resolver can also be constructed from config in tests.
        self._store: SessionIdentityStore | None = None
        if cache is not None:
            self._store = SessionIdentityStore(cache=cache, ttl_seconds=self.config.ttl_seconds)
        self._matcher: HistoryMatcher | None = None
        if self._store is not None:
            self._matcher = HistoryMatcher(store=self._store, config=self.config)

    def _matcher_for(self, cache: Any) -> HistoryMatcher | None:
        """Matcher bound to the cache the hook received (created lazily)."""
        if not self.config.enabled:
            return None
        if cache is None:
            return None
        if self._store is None or self._store.cache is not cache:
            self._store = SessionIdentityStore(cache=cache, ttl_seconds=self.config.ttl_seconds)
            self._matcher = HistoryMatcher(store=self._store, config=self.config)
        return self._matcher

    @staticmethod
    def _model_group(data: dict) -> str:
        model = data.get("model")
        return model if isinstance(model, str) and model else "unknown"

    @staticmethod
    def _caller_scope(user_api_key_dict: Any) -> str:
        """
        Stable caller scope for lineage keys.

        ``UserAPIKeyAuth.api_key`` is already the hashed token (the proxy never
        holds the raw virtual key), and it is the same value
        ``DeploymentAffinityCheck`` hashes/scopes its pins with
        (``metadata.user_api_key_hash``). One hash here keeps the resolver's
        keys aligned with the affinity check's.
        """
        api_key = getattr(user_api_key_dict, "api_key", None)
        if not api_key:
            return "anonymous"
        return str(api_key)

    async def async_pre_call_hook(
        self,
        user_api_key_dict: Any,
        cache: Any,
        data: dict,
        call_type: str,
    ) -> dict:
        if call_type not in _SUPPORTED_CALL_TYPES:
            return data
        matcher = self._matcher_for(cache)
        if matcher is None:
            return data

        _bucket_name, metadata = get_or_create_metadata_bucket(data)
        if isinstance(metadata.get("session_id"), str) and metadata.get("session_id"):
            return data  # client-supplied: affinity already uses it
        if metadata.get("litellm_session_id_generated"):
            return data  # per-request policy id; affinity ignores these
        if isinstance(data.get("litellm_session_id"), str) and data.get("litellm_session_id"):
            return data  # session/vendor header id: already recognized

        model_group = self._model_group(data)
        scope = self._caller_scope(user_api_key_dict)
        declared = declared_id(data)
        try:
            session_id = await matcher.infer_session_id(
                data=data, model_group=model_group, scope=scope, declared=declared
            )
        except Exception:
            return data  # inference must never block or fail a request

        if not session_id:
            return data
        metadata["session_id"] = session_id
        metadata["litellm_session_id_inferred"] = True
        # keep the declared name around so teaching can extend the same lineage
        if declared:
            metadata["litellm_session_identity_declared"] = declared
        return data

    async def async_log_success_event(self, kwargs: dict, response_obj: Any, start_time: Any, end_time: Any) -> None:
        matcher = self._matcher
        if matcher is None or not self.config.enabled:
            return
        metadata = kwargs.get("litellm_params", {}).get("metadata") or kwargs.get("metadata") or {}
        session_id = metadata.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return
        request_data = kwargs.get("litellm_params", {}).get("proxy_server_request", {}) or {}
        body = request_data.get("body") if isinstance(request_data, dict) else None
        data = body if isinstance(body, dict) else kwargs
        if not isinstance(data.get("messages"), list) and not isinstance(kwargs.get("messages"), list):
            return
        messages_source = data if isinstance(data.get("messages"), list) else kwargs
        model_group = self._model_group(messages_source)
        # In the success event the key hash arrives via metadata
        # (litellm_params.metadata.user_api_key_hash is already the hashed
        # token) - use it as the scope directly, matching the hook's scope.
        scope_value = metadata.get("user_api_key_hash")
        scope = str(scope_value) if scope_value else "anonymous"
        declared = metadata.get("litellm_session_identity_declared")
        try:
            await matcher.teach(
                data=messages_source,
                model_group=model_group,
                scope=scope,
                session_id=session_id,
                declared=declared if isinstance(declared, str) else None,
            )
        except Exception:
            pass  # teaching is best-effort; never raise from a logging callback
