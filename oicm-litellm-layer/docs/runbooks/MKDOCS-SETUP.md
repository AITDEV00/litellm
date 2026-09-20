# MkDocs Setup — How This Documentation Site Is Configured

This page documents how the MkDocs site for the OICM → LiteLLM layer is set up,
what files were created, and what steps were taken, so anyone (human or agent)
can rebuild, extend, or troubleshoot it.

## What this site is

A [MkDocs](https://www.mkdocs.org/) static site, themed with
[Material for MkDocs](https://squidfunk.github.io/mkdocs-material/), that serves
as the **quick navigator** for the `oicm-litellm-layer/` directory. It maps where
every component, config, deployment manifest, and credential lives, so you know
exactly which file to edit for a given task.

The site source is the `docs/` directory plus `mkdocs.yml`. The built output
goes to `./site` (gitignored).

## Files created / touched

| File | Purpose |
|------|---------|
| `mkdocs.yml` | The site configuration: theme, plugins, hooks, markdown extensions, nav |
| `requirements-docs.txt` | Build dependencies for the docs venv |
| `docs/index.md` | Home / entry page (quick navigator) |
| `docs/structure.md` | Full directory map (where everything lives) |
| `docs/credentials.md` | Master key / secret contract + rotation runbook |
| `docs/deployment.md` | Deploy / apply / rollout to the cluster |
| `docs/docs-map.md` | Cross-reference of every existing doc |
| `docs/components/*.md` | Per-component pages (controller, config, hooks, custom-routes, patches) |
| `scripts/mkdocs_master_key.py` | MkDocs hook that injects `{{ master_key }}` from the manifest |
| `scripts/get_master_key.py` | stdlib-only extractor read by the Makefile, benchmarks, local env |
| `.gitignore` | Ignore `.venv-docs/` and `oicm-litellm-layer/site/` |
| `Makefile` | `docs` target to install deps + build |

## Steps taken

### 1. Created the build dependencies file

`requirements-docs.txt` pins the two required plugins:

```
mkdocs-material>=9.5
mkdocs-git-revision-date-localized-plugin
```

Install into a dedicated venv (kept separate from the main project venv):

```bash
python3 -m venv .venv-docs
.venv-docs/bin/pip install -r requirements-docs.txt
```

This venv is created automatically by the `make docs` target if missing.

### 2. Wrote `mkdocs.yml`

Key configuration choices:

- **Theme**: `material`, with a light/dark/system auto-switching color scheme and
  a set of navigation features (tabs, sections, instant loading, search
  highlight/suggest/share, top button, footer, tracking, toc follow, code copy).
- **Plugins**:
  - `search` (built-in, client-side search)
  - `git-revision-date-localized` — shows the last-modified / creation date of
    each page from git history, using `date` type and `enable_creation_date: true`.
- **Hooks**: a custom `on_page_markdown` hook
  (`scripts/mkdocs_master_key.py`) that replaces the `{{ master_key }}`
  placeholder in docs with the actual master key value read from the
  `deploy/prod/litellm-proxy.yaml` Secret at build time. This keeps a single source
  of truth — no second copy of the key to keep in sync. The build fails loudly
  if the placeholder is present but the manifest cannot be read.
- **Validation**: strict link/nav validation enabled so broken links and
  omitted files surface as warnings during the build.
- **Markdown extensions**: admonitions, attribute lists, HTML-in-markdown,
  tables, TOC with permalinks, and the PyMdown extensions (highlight,
  inlinehilite, snippets, superfences, tabbed).

### 3. Created the doc pages under `docs/`

- `index.md` — the home page with a one-line summary table and layout overview.
- `structure.md` — the full directory tree with per-file purpose.
- `credentials.md` — the master key contract + rotation runbook.
- `deployment.md` — apply / rollout / cluster access.
- `docs-map.md` — index of every existing doc.
- `components/*.md` — one page per component.

### 4. Strict-build cleanup

The build is strict (validation warnings are surfaced). Two pre-existing broken
doc links were fixed as part of the initial setup so the build passes cleanly:

- `docs/admin-api/LITELLM-ADMIN-REST-API.md`
- `docs/dashboard-plan/LOGIC-MAPPING-TECHNIQUE.md`

### 5. Single source of truth for the master key

Added two scripts so the master key is defined once and derived everywhere:

- `scripts/mkdocs_master_key.py` — MkDocs `on_page_markdown` hook that renders
  the `{{ master_key }}` placeholder from the manifest. Registered in
  `mkdocs.yml` under `hooks:`.
- `scripts/get_master_key.py` — stdlib-only extractor used by the Makefile,
  benchmarks, and local env to read the same value from the manifest.

Controller `config.py` also falls back to the manifest value, and local configs
read `os.environ/LITELLM_MASTER_KEY`. Hardcoded `sk-1234` values in docs were
bulk-replaced with the `{{ master_key }}` placeholder.

### 6. Gitignore the build output

Added to the root `.gitignore`:

```text
.venv-docs
oicm-litellm-layer/site/
```

so the venv and generated site are never committed.

### 7. Added a `make docs` target

```make
docs:
	python3 -m venv .venv-docs 2>/dev/null; \
	.venv-docs/bin/pip install -q -r requirements-docs.txt; \
	.venv-docs/bin/mkdocs build
```

This installs the deps if needed and builds the site in one step.

## Commands

All commands run from `oicm-litellm-layer/`.

**Build the site (one command, idempotent):**

```bash
make docs
```

**Install deps manually:**

```bash
python3 -m venv .venv-docs
.venv-docs/bin/pip install -r requirements-docs.txt
```

**Build:**

```bash
.venv-docs/bin/mkdocs build
```

Output goes to `./site` (gitignored).

**Serve locally for live preview:**

```bash
.venv-docs/bin/mkdocs serve
```

Serves at <http://localhost:8000>.

**Strict validation (surfaces broken links / missing files as warnings):**

Already enabled in `mkdocs.yml` under `validation:`. Any warning you fix by
editing the links keeps future builds clean.

## Adding a new page

1. Create the markdown file under `docs/`.
2. Add it to the `nav:` section of `mkdocs.yml`.
3. If it references the master key, use the `{{ master_key }}` placeholder
   (injected automatically by the hook).
4. Rebuild: `make docs`.

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| Build fails on `{{ master_key }}` | `deploy/prod/litellm-proxy.yaml` is missing, or the `litellm-master-key` Secret has no non-empty `master-key` entry. Fix the manifest (or remove the placeholder). |
| `git-revision-date-localized` shows no dates | The page is not committed yet, or the plugin cannot read git history for it. |
| Link validation warnings | A referenced page/anchor is missing. Open the warning path and fix the link. |
| `mkdocs` command not found | Activate the docs venv first or use the full path `.venv-docs/bin/mkdocs`. |

## Installed versions (reference)

Pinned by `requirements-docs.txt` at the time of writing:

- mkdocs 1.6.1
- mkdocs-material 9.7.7
- mkdocs-git-revision-date-localized-plugin 1.5.3
- Markdown 3.10.3, Pygments 2.20.0, pymdown-extensions 11.0.1