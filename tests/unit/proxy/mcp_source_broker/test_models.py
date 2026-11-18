from __future__ import annotations

import pytest
from pydantic import ValidationError

from litellm.proxy.mcp_source_broker.models import SourceCreate, Target

def test_alias_rejects_the_tool_name_separator() -> None:
    """The alias becomes the MCP server name, which prefixes every tool name.

    '-'(MCP_TOOL_PREFIX_SEPARATOR) would make that prefix ambiguous, so it has
    to be rejected at the schema even though it is a natural alias character.
    """
    base = {
        "owner": "alice",
        "target": Target(),
        "spec": '{"openapi":"3.1.0","paths":{"/v1/pets":{"get":{}}}}',
    }
    assert SourceCreate(alias="pets_api", **base).alias == "pets_api"
    with pytest.raises(ValidationError):
        SourceCreate(alias="pets-api", **base)
