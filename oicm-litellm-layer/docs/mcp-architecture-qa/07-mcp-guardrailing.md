# MCP guardrailing: parameter input and whole-tool denial

Direct answer to "how is MCP parameter input guardrailed, and can a whole tool be denied based on conversation trust state". All three capabilities exist in this source tree. The 2026-11-23 inventory found no guardrails configured in prod; recheck the live configuration before treating that dated absence as current

Related: [`06-permissions-and-scope.md`](06-permissions-and-scope.md) for the access layers, including its correction that public-address eligibility is separate from authorization

---

## 1. Parameter input guardrailing

Three mechanisms, each catching a different class of problem. They compose; each only narrows.

### 1a. Fixed argument allowlist per tool: `allowed_params`

- Code: `MCPManager.validate_allowed_params` in `litellm/proxy/_experimental/mcp_server/mcp_server_manager.py`.
- Config shape: `server.allowed_params = {"<bare_tool_name>": ["param_a", "param_b", ...]}`.
- Behavior: every argument key not in the list raises `HTTPException 403` before the tool is dispatched, and the error names both the disallowed params and the allowed set so an operator can fix a caller.

```
Parameters ['evil'] are not allowed for tool web_search. Allowed parameters: ['q']. Contact proxy admin to allow these parameters.
```

- Semantics to know: this is a **strict allowlist**, not a deny-list. If you enable it on a tool you must enumerate every parameter the model legitimately needs, including optional ones. A tool with no entry in `allowed_params` is unrestricted.
- Lookup: `match_known_tool_name` normalizes the caller's spelling against the server's registered prefixes, so the key is the bare tool name, not the wire-prefixed one.
- Prod state: `allowed_params: null` on all eight servers, so nothing is restricted.

### 1b. Content scanning of argument strings: `MCPGuardrailTranslationHandler.process_input_messages`

- Code: `litellm/proxy/_experimental/mcp_server/guardrail_translation/handler.py`.
- Registration: `guardrail_translation/__init__.py` maps `CallTypes.call_mcp_tool` and `CallTypes.list_mcp_tools` to this handler, so it fires on both listing and calling.
- What it collects into one `GenericGuardrailAPIInputs`:

| Source in the MCP call | Scanned? |
|---|---|
| tool description | yes, as one text |
| every `description` string leaf of `inputSchema` | yes, deep-walked and filtered on the final path key |
| every string leaf of the arguments | yes, deep-walked |
| keys and non-string values | no, they are not maskable text |

- Then it calls `guardrail_to_apply.apply_guardrail(...)`, which means **any** `CustomGuardrail` plugs in: presidio for PII, a prompt-injection classifier, a language filter, a bespoke class.
- Write-back: masked text is written back into the arguments and into the schema description leaves, so the outbound MCP call carries the masked value, not the original. `data["modified_arguments"]` exposes the applied mask to the caller.
- Rejection: if the guardrail raises, the call never happens.
- Safety properties worth citing:
  - A depth limit on the JSON walk, raising rather than scanning shallowly and missing nested strings.
  - Conflicting rewrites of the same leaf path raise `_conflicting_rewrite` instead of silently picking one.
  - The filter includes `description` strings only, not arbitrary schema strings such as `title`, `default`, or `enum` values. Tool descriptions are model-visible and can carry payloads; keep this field selection explicit when designing a guardrail

### 1c. Output and result scanning: `process_output_response`

Same handler, for the tool result. Scans `content` text blocks and `structuredContent` string leaves, masks in place (not a copy) so the spend log and span do not keep the unmasked text, and raises if a masking rule matched a non-rewritable field such as a structured key or a numeric value. A result the guardrail rejects propagates the rejection to the caller.

---

## 2. Whole-tool denial based on conversation trust state: `tool_policy`

- Code: `litellm/proxy/guardrails/guardrail_hooks/tool_policy/tool_policy_guardrail.py`.
- It is a `CustomGuardrail` registered as `guardrail: tool_policy`, typically `mode: post_call` in the `guardrails:` config list.
- It reads two policy sources:

| Source | Field | Values |
|---|---|---|
| tool registry | `LiteLLM_ToolTable.input_policy` | `untrusted` (default), `trusted`, `blocked` |
| tool registry | `LiteLLM_ToolTable.output_policy` | `untrusted` (default), `trusted` |
| key/team object permission | `blocked_tools: [name, ...]` | a deny list |

Behavior:

- `blocked` on input policy: raise `HTTPException`, the tool call never executes.
- `trusted` on input policy: the tool is allowed **only if the conversation contains no untrusted tool output**, which is the trust-state reasoning. One tool returning untrusted output taints the conversation for a later `trusted`-gated tool.
- `untrusted`: pass through, the default for a newly discovered tool.
- `blocked_tools` on a key or team is an independent hard deny by tool name, resolved per permission scope in `tool_registry_writer.py`.

This is the only mechanism in the tree that reasons about provenance of earlier tool output, so it is the answer to "deny a whole tool based on conversation trust state". Prod has zero guardrails configured, so it is off.

---

## 3. What is NOT native

- **JSON Schema validation of arguments against `inputSchema`.** The gateway applies `allowed_params` and guardrail scanners, then forwards arguments to the MCP server, which validates them. The gateway does not re-validate types, required-ness, or `additionalProperties` itself. If you need that, an argument-validating `CustomGuardrail` in `process_input_messages` is the seam, and the schema is passed in the same call as `mcp_input_schema`, so the validator has everything it needs without a second lookup.
- **Per-tool rate limits or per-tool budgets.** Not a guardrail concern in this layer; server-level `rpm` / `max_concurrent_requests` exist but are not tool-scoped.

---

## 4. Choosing between them

| You want | Use | Why |
|---|---|---|
| Never let a param reach the origin | `allowed_params` | cheapest, exact, no guardrail config |
| Catch PII, secrets, injection text in args | guardrail scanner via `process_input_messages` | content-aware, masks rather than blocks when possible |
| Stop a dangerous tool after poisoned context | `tool_policy` `blocked` / `trusted` | the only provenance-aware control |
| Hide a tool entirely from one team | `mcp_tool_permissions` / toolset, or `blocked_tools` | tool-level scope, not content |

## Source files

- `litellm/proxy/_experimental/mcp_server/mcp_server_manager.py` `validate_allowed_params`, `check_allowed_or_banned_tools`
- `litellm/proxy/_experimental/mcp_server/guardrail_translation/handler.py`
- `litellm/proxy/_experimental/mcp_server/guardrail_translation/__init__.py`
- `litellm/proxy/guardrails/guardrail_hooks/tool_policy/tool_policy_guardrail.py`
- `litellm/proxy/db/tool_registry_writer.py` `blocked_tools` resolution
- `litellm/types/object_permission.py` `blocked_tools`
- `litellm/proxy/schema.prisma` `blocked_tools`, `allowed_params`
