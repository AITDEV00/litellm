"""MkDocs on_page_markdown hook that injects the LiteLLM master key value.

The single source of truth for the admin/master key is the inline Kubernetes
Secret in ``deploy/litellm-proxy.yaml`` (``litellm-master-key`` /
``master-key``). Docs reference it via the ``{{ master_key }}`` placeholder;
this hook reads the value from that manifest at build time so the docs always
mirror the deployed value and there is no second copy to keep in sync.

The build fails loudly if the placeholder is present but the manifest cannot be
read, so a misconfigured source can never silently emit a stale key.
"""

from __future__ import annotations

from pathlib import Path

import yaml

PLACEHOLDER = "{{ master_key }}"
# Relative to the oicm-litellm-layer/ directory (where mkdocs.yml lives).
MANIFEST_RELATIVE = Path("deploy/litellm-proxy.yaml")
SECRET_NAME = "litellm-master-key"
SECRET_KEY = "master-key"


def _master_key_from_manifest(manifest: Path) -> str:
    docs = yaml.safe_load_all(manifest.read_text(encoding="utf-8"))
    for document in docs:
        if not isinstance(document, dict):
            continue
        if document.get("kind") != "Secret":
            continue
        metadata = document.get("metadata") or {}
        if metadata.get("name") != SECRET_NAME:
            continue
        string_data = document.get("stringData") or {}
        value = string_data.get(SECRET_KEY)
        if isinstance(value, str) and value.strip():
            return value.strip()
        raise RuntimeError(
            f"Secret {SECRET_NAME!r} in {manifest} has no non-empty "
            f"{SECRET_KEY!r} entry. Refusing to render {PLACEHOLDER}."
        )
    raise RuntimeError(
        f"Could not find Secret {SECRET_NAME!r} in {manifest}. "
        f"Refusing to render {PLACEHOLDER}."
    )


def on_page_markdown(markdown: str, *, page, config, files) -> str:
    if PLACEHOLDER not in markdown:
        return markdown

    manifest = Path(config["docs_dir"]).resolve().parent / MANIFEST_RELATIVE
    if not manifest.is_file():
        raise RuntimeError(
            f"Source of truth {manifest} not found while rendering {PLACEHOLDER} "
            f"in {page.file.src_path}. Fix the manifest path or remove the placeholder."
        )
    value = _master_key_from_manifest(manifest)
    return markdown.replace(PLACEHOLDER, value)