# Documentation guidance

## Read first

For any question about this component, begin at [index.md](index.md). It routes task names and symptoms to owning documents. Use [document-status.md](document-status.md) to distinguish current references, plans, dated deployment observations, evidence, historical material, and generated OpenAPI pages. Current source and deployed configuration always outrank documentation claims

## Ownership map

The physical folders group component families. The `mkdocs.yml` navigation stays organized around reader tasks and does not need to reproduce this directory tree exactly

- `platform/`: OICM component surfaces, discovery controller, model pricing, OICM status, OpenRouter compatibility, and custom code origins
- `administration/`: admin REST and request-schema reference, access groups, team and key ownership, RBAC, SSO, JWT integration, and cache invalidation
- `providers/`: custom-provider implementation, provider diagnostics, custom routes, and maintainer design work
- `mcp/`: MCP runtime, topology, guardrails, OpenCode, and the `source-broker/` implementation, decisions, measurements, transport, and history
- `distribution/`: same-origin CLI and source-connector artifacts, storage, publication, security, and client releases
- `guides/`: consumer invocation guides for models and voice APIs, plus MCP client installation and operation
- `operations/`: deployment, credentials, TLS scripts, runbooks, incident timelines, rate limiting, performance analysis, and supporting evidence
- `engineering/`: integration architecture, dashboard workflows, session identity, persona drift, and reusable investigation techniques

Do not merge consumer guides with provider implementation research. Keep open incident records separate from durable performance analysis. Within the MCP source broker, do not flatten `history/` automatically: its app-side gateway execution guide describes adjacent behavior rather than an entirely superseded design

The old `mcp-architecture-qa/` is deliberately retired. Do not recreate it or start other conversation-scoped Q&A scratch directories. Incorporate durable questions into the owning reference page or give one coherent topic its own page

## Keep docs usable

Write about behavior, not about how a document was produced. Documents named with a date or evidence numbers are usually observations, not permanent contracts. Do not silently replace a past production read with an unverified assertion about the present

When adding, moving, or renaming a page:

1. Put it in the owning directory, and update the owning directory's index with a trust label
2. Update [docs-map.md](docs-map.md) with its purpose and path
3. Add it to `../mkdocs.yml` `nav:`, or link it from an owning register when it is generated evidence inventory
4. Repair inbound links and verify the resolved old and new Markdown source paths. Do not change arbitrary matching filenames in prose or code
5. Add an `mkdocs-redirects` mapping for every moved Markdown page, preserving the old source path as its key
6. Build using `.venv-docs/bin/mkdocs build` from `oicm-litellm-layer/` and verify the old URL redirects and the new page renders

Do not hand-edit generated files under `administration/admin-api/operations/`. They are snapshots tied to the documented gateway release. Verify OpenAPI route presence does not prove enabled production configuration or an authorized operation

## Editing workflow

Use `git mv` for moves so the file history follows content. If unrelated edits already exist in the working tree, preserve them, identify them in review, and do not revert or conceal them. Check `git diff --cached --stat` before committing

Run the full docs build and inspect all warning output. Links resolving outside `docs/` to source files or runnable scripts are filesystem-checked separately because MkDocs does not index them. Do not ignore warnings about missing internal pages or anchors. For an old `README.md` redirect, verify the directory URL, such as `/old/topic/`, rather than `/old/topic/README/`
