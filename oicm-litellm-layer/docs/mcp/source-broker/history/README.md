# History

## Scope

Retired designs and material that describes a scope other than the broker. Nothing here is current behavior. These files are kept because their reasoning, measurements, or scope boundaries are still worth citing.

## Contents

| File | Why it is here |
|---|---|
| [`superseded-design-proposal.md`](superseded-design-proposal.md) | The original pre-build plan. Three mechanisms were retired during implementation: Chisel, the pod-hosted relay, and the Chisel client installer |
| [`app-side-mcp-scope/gateway-mcp-execution-guide.md`](app-side-mcp-scope/gateway-mcp-execution-guide.md) | Describes application-side MCP execution, a different behavior from the broker. It lives here because it was written alongside the broker work and is still the reference for the gateway-owned tool namespace |

## Warning

`superseded-design-proposal.md` names routes, ports, and a transport that do not exist in the tree. Do not implement from it. Its own header states which sections are wrong and points at the owning record.

## Rule for this slice

A file belongs in `history/` when it would mislead someone about current behavior if read without context. If a retired design is fully replaced and nothing cites it, delete it instead of filing it here. History is for reasoning that is still referenced, not for everything ever written.
