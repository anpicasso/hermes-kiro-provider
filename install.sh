#!/usr/bin/env bash
# Install both Hermes Kiro plugin surfaces. No gateway restart is performed.
set -euo pipefail

SOURCE="anpicasso/hermes-plugin-kiro"

if ! command -v hermes >/dev/null 2>&1; then
  printf '%s\n' 'Hermes CLI was not found in PATH. Install Hermes first.' >&2
  exit 1
fi

hermes plugins install "$SOURCE/commands" --no-enable
hermes plugins install "$SOURCE/provider" --no-enable
hermes plugins enable kiro

cat <<'EOF'

Kiro for Hermes is installed.

Start a new terminal session, then authenticate:
  hermes kiro login

If the Hermes gateway is already running, restart it once so it loads the provider:
  systemctl --user restart hermes-gateway

The installer did not restart the gateway.
EOF
