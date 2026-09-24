"""SessionIdentityResolver: infer a stable session id when the client sends none.

A CustomLogger pre-call callback, run before route_request -> Router.acompletion
-> DeploymentAffinityCheck. Whatever it stamps into ``metadata["session_id"]``
is what the affinity machinery pins on. LiteLLM stays the only router.

Precedence (highest first):
1. explicit ``metadata.session_id``          client-owned, pass through
2. declared ``prompt_cache_key`` / ``conversation``, resolved WITHOUT Redis
3. history lineage                            recover a previously taught id
4. synthesized                                fresh id for a new conversation

A policy-generated id is treated as absent (DeploymentAffinityCheck ignores
marked ids): the value is preserved under a side key, the marker removed, and an
inferred id stamped in its place.

Lineage is TAUGHT in this pre-call hook, synchronously, not in the success
callback: success logging is deferred off the request-critical path
(GLOBAL_LOGGING_WORKER), so a fast client could send turn 2 before turn 1's
teach lands, miss the lookup, and get a new id — defeating the pin. Teaching
here adds one Redis pipeline before routing but makes turn 1's lineage visible
to turn 2. The success callback is therefore unnecessary and removed.

The hook never mutates ``data``: it returns a new dict carrying the updated
metadata bucket (copy-on-write). Covers only the OpenAI chat surface
(``completion``/``acompletion``); ``/v1/messages`` and Responses bypass this hook.
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
from litellm.router_utils.session_identity.views import RequestView, project_request

verbose_logger: Final = logging.getLogger("litellm")

_SUPPORTED_CALL_TYPES: Final = frozenset({"completion", "acompletion"})

_META_SCOPE: Final = "_session_identity_scope"
_META_MODEL_GROUP: Final = "_session_identity_model_group"
_META_SOURCE: Final = "_session_identity_source"
_META_DECLARED: Final = "litellm_session_identity_declared"
_META_INFERRED: Final = "litellm_session_id_inferred"
_META_POLICY_GENERATED: Final = "litellm_session_id_policy_generated"

_GENERATED_KEYS: Final = frozenset({"session_id", SESSION_ID_GENERATED_METADATA_KEY})


class SessionIdentityResolver(CustomLogger):
    def __init__(self, config: SessionIdentityConfig | None = None, cache: Any = None):
        self.config: Final = config or SessionIdentityConfig()
        self._store: SessionIdentityStore | None = (
            SessionIdentityStore(cache=cache, ttl_seconds=self.config.ttl_seconds) if cache is not None else None
        )

    def _store_for(self, cache: Any) -> SessionIdentityStore | None:
        if not self.config.enabled or cache is None:
            return None
        if self._store is None or self._store.cache is not cache:
            self._store = SessionIdentityStore(cache=cache, ttl_seconds=self.config.ttl_seconds)
        return self._store

    @staticmethod
    def _model_group(request: RequestView) -> str:
        model: Final = request.get("model")
        return model if isinstance(model, str) and model else "unknown"

    @staticmethod
    def _caller_scope(user_api_key_dict: Any) -> str:
        # UserAPIKeyAuth.api_key is already the hashed token, the same value
        # DeploymentAffinityCheck scopes its pins with; used directly.
        api_key: Final = getattr(user_api_key_dict, "api_key", None)
        return str(api_key) if api_key else "anonymous"

    async def _shadow_teach(
        self, store: SessionIdentityStore, request: RequestView, model_group: str, scope: str, session_id: str
    ) -> None:
        """Record history under an explicit/header session id (best-effort), so a
        later request that drops the id recovers the same affinity identity."""
        try:
            chain: Final = build_chain(
                request=request,
                model_group=model_group,
                cache_salt=self.config.cache_salt,
                chunk_size=self.config.chunk_size_bytes,
            )
            if chain:
                await store.teach(chain=chain, session_id=session_id, model_group=model_group, scope=scope)
        except Exception as e:  # noqa: BLE001  # fail-open: teaching must never block a request
            verbose_logger.warning("session_identity: shadow teach failed: %s", e)

    async def _resolve(
        self, store: SessionIdentityStore, request: RequestView, model_group: str, scope: str
    ) -> IdentityResolution | None:
        declared: Final = declared_id(request)
        if declared is not None:
            return IdentityResolution(
                session_id=scoped_declared_session_id(declared, model_group, scope),
                matched_depth=0,
                source="declared",
                declared=declared,
            )
        chain: Final = build_chain(
            request=request,
            model_group=model_group,
            cache_salt=self.config.cache_salt,
            chunk_size=self.config.chunk_size_bytes,
        )
        if not chain:
            return None
        match: Final = await store.lookup(chain=chain, model_group=model_group, scope=scope)
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
        store: Final = self._store_for(cache)
        if store is None:
            return data

        try:
            request: Final = project_request(data)
        except Exception as e:  # noqa: BLE001  # fail-open: inference must never block a request
            verbose_logger.warning("session_identity: request projection failed, routing normally: %s", e)
            return data
        model_group: Final = self._model_group(request)
        scope: Final = self._caller_scope(user_api_key_dict)

        metadata_key, metadata = get_or_create_metadata_bucket(data)
        existing: Final = metadata.get("session_id")
        generated: Final = bool(metadata.get(SESSION_ID_GENERATED_METADATA_KEY))
        header_id: Final = data.get("litellm_session_id")

        # Every selection teaches its history, so a later request that loses its
        # id can still recover the same affinity identity (vLLM Router #219).
        if isinstance(existing, str) and existing and not generated:
            await self._shadow_teach(store, request, model_group, scope, existing)
            return data  # client-supplied: affinity already uses it
        if isinstance(header_id, str) and header_id:
            await self._shadow_teach(store, request, model_group, scope, header_id)
            return data  # session/vendor header id: already recognized

        try:
            resolution: Final = await self._resolve(store=store, request=request, model_group=model_group, scope=scope)
        except Exception as e:  # noqa: BLE001  # fail-open: inference must never block a request
            verbose_logger.warning("session_identity: resolution failed, routing normally: %s", e)
            return data

        if resolution is None or not resolution.session_id:
            return data

        # Teach synchronously so the next request's pre-call lookup sees it. A
        # fresh synthesized identity is only recoverable if this write persists;
        # if it doesn't, fail open rather than pin an unrecoverable session id.
        try:
            chain: Final = build_chain(
                request=request,
                model_group=model_group,
                cache_salt=self.config.cache_salt,
                chunk_size=self.config.chunk_size_bytes,
            )
            if chain:
                persisted: Final = await store.teach(
                    chain=chain,
                    session_id=resolution.session_id,
                    model_group=model_group,
                    scope=scope,
                    start_index=resolution.matched_depth,
                )
                if resolution.source == "synthesized" and not persisted:
                    verbose_logger.warning(
                        "session_identity: synthesized lineage not persisted; routing without an inferred id"
                    )
                    return data
        except Exception as e:  # noqa: BLE001  # fail-open: teaching must never block a request
            verbose_logger.warning("session_identity: teach failed: %s", e)
            if resolution.source == "synthesized":
                return data

        removals: Final = _GENERATED_KEYS if generated else frozenset()
        new_metadata: Final = {
            **{k: v for k, v in metadata.items() if k not in removals},
            "session_id": resolution.session_id,
            _META_INFERRED: True,
            _META_SCOPE: scope,
            _META_MODEL_GROUP: model_group,
            _META_SOURCE: resolution.source,
            **({_META_DECLARED: resolution.declared} if resolution.declared else {}),
            **({_META_POLICY_GENERATED: existing} if generated and isinstance(existing, str) else {}),
        }
        return {**data, metadata_key: new_metadata}
