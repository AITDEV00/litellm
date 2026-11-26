# Documentation guidance

## Read first

For any question about this component, begin at [index.md](index.md). It routes task names and symptoms to owning documents. Use [document-status.md](document-status.md) to distinguish current references, plans, dated deployment observations, evidence, historical material, and generated OpenAPI pages. Current source and deployed configuration always outrank documentation claims

## Ownership map

- `admin-api/`: admin REST and request-schema reference, operating capability map and team/access-group playbook
- `access-control/`: authorization, team and key scopes, RBAC, identities, SSO, and JWT integration
- `mcp/` and `mcp-source-broker/`: MCP topology, connector flow, source broker, guardrails, decisions, and private skills
- `usage-guides/` and `custom-providers/`: external-facing API use and custom-provider design
- `components/`, `discovery-controller/`, `model-pricing/` and `oicm-status/`: OICM gateway/controller surfaces
- `deployment.md`, `credentials.md`, `runbooks/`, `incidents/`, `performance/`, and `htb-rate-limiting/`: operations and evidence
- `client-distribution/`: CLI and connecter distribution
- `techniques/`: reusable investigation and engineering methods

The old `mcp-architecture-qa/` is deliberately retired. Do not recreate it or start other conversation-scoped Q&A scratch directories. Incorporate durable questions into the owning reference page or give one coherent topic its own page

## Keep docs usable

Write about behavior, not about how a document was produced. Documents named with a date or evidence numbers are usually observations, not permanent contracts. Do not silently replace a past production read with an unverified assertion about the present

When adding, moving, or renaming a page:

1. Put it in the owning directory, and update the owning directory's index with a trust label
2. Update [docs-map.md](docs-map.md) with its purpose and path
3. Add it to `../mkdocs.yml` `nav:`, or link it from an owning register when it is generated evidence inventory
4. Repair all inbound Markdown links, including references in pages that did not move
5. Build using `.venv-docs/bin/mkdocs build` from `oicm-litellm-layer/`

Do not hand-edit generated files under `admin-api/operations/`. They are snapshots tied to the documented gateway release. Verify OpenAPI route presence does not prove enabled production configuration or an authorized operation

## Editing workflow

Use `git mv` for moves so the file history follows content. If unrelated edits already exist in the working tree, preserve them, identify them in review, and do not revert or conceal them. Check `git diff --cached --stat` before committing

Run the full docs build, then inspect warning output for links inside the files you touched. Baseline site warnings may exist elsewhere; unrelated missing external source-document links are not repaired by moving their surrounding pages
