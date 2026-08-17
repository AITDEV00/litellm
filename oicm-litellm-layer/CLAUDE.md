# OICM → LiteLLM Integration Layer — Coding Guidelines

Layer-specific rules on top of the repo-root `CLAUDE.md` and `AGENTS.md`.

## Docs: navigation starts with mkdocs

The source of truth for documentation structure in this layer is
`oicm-litellm-layer/mkdocs.yml`. Before writing, editing, or moving any doc,
read the `nav:` section of `mkdocs.yml` to see where things are supposed to
live.

Rules:

1. **Start from `mkdocs.yml`.** If you are adding or changing documentation,
   first read the `nav:` block to determine the correct section and filename.
   Do not invent a location from scratch.
2. **Place every new doc in the correct location** under
   `oicm-litellm-layer/docs/<topic>/`, matching an existing nav section when
   one applies. Runnable scripts and examples belong in
   `oicm-litellm-layer/examples/`, **not** in `docs/`.
3. **Update `mkdocs.yml` on every docs change.** Whenever you add a new doc
   file, add a matching entry to the `nav:` section. Whenever you rename, move,
   or delete a doc, update the nav to match. Never leave the nav stale.
4. **Update `oicm-litellm-layer/docs/docs-map.md`** alongside mkdocs.yml so the
   human-readable map stays in sync.
5. **Do not write docs to the repo-root `docs/` directory.** That is the
   upstream LiteLLM docs tree, intentionally removed from tracking (see commit
   `adf6eb75a4`); it is not part of this layer's site.
6. **Validate the build.** After nav changes, run
   `make docs` (or `.venv-docs/bin/mkdocs build`) from `oicm-litellm-layer/`.
   The `validation.nav` block (omitted_files / not_found: warn) surfaces
   mistakes. `--strict` aborts only on real nav/link errors, not on the
   `git-revision-date-localized` quirk for newly-added untracked files.