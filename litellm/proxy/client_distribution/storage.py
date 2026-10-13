"""RustFS (S3-compatible) object-store client for the client-distribution slice.

Reads its configuration from environment variables (set in the Deployment
manifest, Phase 3). Creates a plain ``boto3.client("s3", endpoint_url=...)``
matching how ``litellm.integrations.s3`` builds its own client.

The store is per-environment: dev talks to ``rustfs-dev``, prod to ``rustfs``.
The bucket name is fixed (``litellm-client-mirror`` / ``litellm-client-mirror-dev``)
via the env var.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client
    from mypy_boto3_s3.type_defs import GetObjectOutputTypeDef

_MIRROR_PREFIX: Final = "litellm-client-mirror"


@dataclass(frozen=True, slots=True)
class RustFSConfig:
    """Connection parameters for the RustFS store."""

    endpoint_url: str
    access_key: str
    secret_key: str
    bucket: str


def load_config_from_env() -> RustFSConfig:
    """Read RustFS connection parameters from the environment.

    Raises ``RuntimeError`` if any required variable is missing.
    """
    endpoint_url: str | None = os.environ.get("RUSTFS_ENDPOINT_URL")
    access_key: str | None = os.environ.get("RUSTFS_ACCESS_KEY")
    secret_key: str | None = os.environ.get("RUSTFS_SECRET_KEY")
    bucket: str | None = os.environ.get("RUSTFS_BUCKET", _MIRROR_PREFIX)

    missing: list[str] = []
    if not endpoint_url:
        missing.append("RUSTFS_ENDPOINT_URL")
    if not access_key:
        missing.append("RUSTFS_ACCESS_KEY")
    if not secret_key:
        missing.append("RUSTFS_SECRET_KEY")
    if missing:
        raise RuntimeError(
            f"client_distribution: missing env vars: {', '.join(missing)}"
        )

    return RustFSConfig(
        endpoint_url=endpoint_url,  # type: ignore[arg-type]  # narrowed above
        access_key=access_key,  # type: ignore[arg-type]
        secret_key=secret_key,  # type: ignore[arg-type]
        bucket=bucket,
    )


def create_s3_client(config: RustFSConfig) -> S3Client:
    """Build a boto3 S3 client pointed at RustFS."""
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=config.endpoint_url,
        aws_access_key_id=config.access_key,
        aws_secret_access_key=config.secret_key,
        region_name="us-east-1",
    )


def normalize_key(path: str) -> str:
    """Normalize a request path to a bucket-relative key.

    Strips leading slashes, rejects ``..`` traversal, and returns the key
    relative to the mirror bucket. Raises ``ValueError`` on escape attempts.
    """
    cleaned: str = path.lstrip("/")
    if not cleaned:
        raise ValueError("empty path")
    parts: list[str] = []
    for segment in cleaned.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            raise ValueError(f"path traversal rejected: {path!r}")
        parts.append(segment)
    if not parts:
        raise ValueError("empty path after normalization")
    return "/".join(parts)


def sha256_stream(stream: object, chunk_size: int = 65536) -> str:
    """Compute the SHA-256 hex digest of a readable stream."""
    h: hashlib._Hash = hashlib.sha256()  # type: ignore[attr-defined]
    while True:
        chunk: bytes = stream.read(chunk_size)  # type: ignore[union-attr]
        if not chunk:
            break
        h.update(chunk)
    return h.hexdigest()
