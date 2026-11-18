#!/bin/sh
# Source connector installer.
#
# Fetches and checksum-verifies the release assets, then runs the binary's own
# interactive commands, which are where every question is asked and every file is
# written. This script deliberately contains no configuration logic: prompts,
# authentication, enrollment and identity persistence all live in the binary, so a
# change to any of them needs no new script.
#
# The agent keeps a local account store, so installing means two steps: add the
# gateway account once (gateway URL, username, password), then add one or more
# sources under it. A second source never asks for the password again. Re-running
# this installer on an installed host is safe: an existing account is reported and
# the source step is skipped by the agent's own already-exists check.
#
# Placeholders are substituted at publish time by the release publisher.
set -eu
umask 077

state="${MCP_SOURCE_LINK_STATE_DIR:-$HOME/.config/source-agent}"
mkdir -p "$state" "$HOME/.config/systemd/user"
chmod 700 "$state" "$HOME/.config/systemd/user"

base="__BASE__"
version="__VERSION__"
expected="__SHA__"

for asset in source-link release.json; do
  curl --fail --location --silent --show-error \
    --output "$state/$asset.new" "$base/install/source-agent/$version/$asset"
  mv -f "$state/$asset.new" "$state/$asset"
done

actual=$(sha256sum "$state/source-link" | cut -d ' ' -f 1)
[ "$actual" = "$expected" ] || { echo 'source connector checksum mismatch' >&2; exit 1; }
chmod 500 "$state/source-link"

cat > "$HOME/.config/systemd/user/source-agent.service" <<'UNIT'
[Unit]
Description=ADEO MCP source connector (tunnel)

[Service]
Restart=always
RestartSec=2s
ExecStart=__STATE__/source-link connector
NoNewPrivileges=yes

[Install]
WantedBy=default.target
UNIT

export MCP_SOURCE_LINK_STATE_DIR="$state"

echo "Step 1: add the gateway account (asked once)."
"$state/source-link" account add

echo "Step 2: add a source under that account."
"$state/source-link" source add

systemctl --user daemon-reload
systemctl --user enable --now source-agent.service

echo "Source connector installed. Service: source-agent.service"
echo "State directory: $state"
echo "Add another source later with: $state/source-link source add"
