"""Typed boundary for session identity.

Two seams touch LiteLLM data untyped at the source: the proxy request body and
the DualCache backends. Each is validated/narrowed once here; the rest of the
package works against strongly typed values (LiteLLM's rule: validate untyped
input with a TypeAdapter at the caller, then pass the typed value in).
"""

from collections.abc import Mapping, Sequence
from typing import Final, Protocol, TypeAlias

import orjson
from pydantic import ConfigDict, TypeAdapter, ValidationError
from typing_extensions import NotRequired, ReadOnly, TypedDict


class MessageView(TypedDict):
    role: ReadOnly[str]
    content: ReadOnly[NotRequired[object]]
    name: ReadOnly[NotRequired[str]]
    tool_call_id: ReadOnly[NotRequired[str]]
    tool_calls: ReadOnly[NotRequired[object]]
    function_call: ReadOnly[NotRequired[object]]


class RequestView(TypedDict):
    model: ReadOnly[NotRequired[str]]
    messages: ReadOnly[NotRequired[tuple[MessageView, ...]]]
    tools: ReadOnly[NotRequired[object]]
    # typed `object` (not `str`): an odd-typed value must project cleanly and be
    # ignored by declared_id's isinstance check, not blow up the hook.
    prompt_cache_key: ReadOnly[NotRequired[object]]
    conversation: ReadOnly[NotRequired[object]]

    __pydantic_config__ = ConfigDict(extra="ignore")


_REQUEST_ADAPTER: Final = TypeAdapter(RequestView)


def project_request(data: object) -> RequestView:
    """Validate and narrow an untyped request body to the fields this feature consumes."""
    return _REQUEST_ADAPTER.validate_python(data)


class LineageRecord(TypedDict):
    session_id: ReadOnly[str]
    chain_len: ReadOnly[int]


_LINEAGE_ADAPTER: Final = TypeAdapter(LineageRecord)


def parse_lineage_record(value: object) -> LineageRecord | None:
    """One stored lineage value, or None if malformed. A single corrupt node is
    skipped by the caller rather than aborting the whole lookup."""
    try:
        raw: Final = orjson.loads(value) if isinstance(value, (str, bytes)) else value
        return _LINEAGE_ADAPTER.validate_python(raw)
    except (orjson.JSONDecodeError, ValidationError):
        return None


# The two LiteLLM cache backends disagree on read shape: Redis returns a Mapping
# keyed by cache key, the in-memory cache returns a positional list.
CacheBatchResult: TypeAlias = Mapping[str, object] | Sequence[object] | None


class LineageCacheReader(Protocol):
    async def async_batch_get_cache(self, cache_keys: Sequence[str]) -> CacheBatchResult: ...
