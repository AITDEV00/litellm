#!/bin/sh
set -eu

readonly ARTIFACT_DIRECTORY="${1:-$(dirname "$0")/../deploy/overlays/dev/mcp-source-broker}"
readonly VERSION=1.12.0
readonly SHA256=f3f180f1d93aa72cce4e6386f98cc06569a0146fbd65eb4423cf83e6434bcfe6
readonly ARCHIVE="$ARTIFACT_DIRECTORY/chisel.gz"

mkdir -p "$ARTIFACT_DIRECTORY"
curl --fail --location --proto '=https' --tlsv1.2 \
  --output "$ARCHIVE.download" \
  "https://github.com/jpillora/chisel/releases/download/v${VERSION}/chisel_${VERSION}_linux_amd64.gz"
printf '%s  %s\n' "$SHA256" "$ARCHIVE.download" | sha256sum --check --strict
gzip --test "$ARCHIVE.download"
chmod 600 "$ARCHIVE.download"
mv "$ARCHIVE.download" "$ARCHIVE"
printf 'Verified Chisel %s linux/amd64 at %s\n' "$VERSION" "$ARCHIVE"
