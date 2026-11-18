#!/bin/sh
# Source connector installer.
#
# Fetches and checksum-verifies the release assets, then runs the binary's own
# interactive initializer, which is where every question is asked and every file
# is written. This script deliberately contains no configuration logic: prompts,
# authentication, enrollment and identity persistence all live in the binary's
# `init` command, so a change to any of them needs no new script.
#
# Placeholders are substituted at publish time by the release publisher.
set -eu
umask 077

state="${SOURCE_AGENT_STATE:-$HOME/.config/source-agent}"
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

"$state/source-link" init

systemctl --user daemon-reload
systemctl --user enable --now source-agent.service

echo "Source connector installed. Service: source-agent.service"
echo "State directory: $state"
