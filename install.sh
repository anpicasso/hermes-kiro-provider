#!/usr/bin/env bash
# Install both Hermes Kiro plugin surfaces. No gateway restart is performed.
set -euo pipefail

SOURCE="anpicasso/hermes-plugin-kiro"
HERMES_HOME_DIR="${HERMES_HOME:-$HOME/.hermes}"

if ! command -v hermes >/dev/null 2>&1; then
  printf '%s\n' 'Hermes CLI was not found in PATH. Install Hermes first.' >&2
  exit 1
fi

install_if_missing() {
  local name="$1" source="$2"
  if [ -d "$HERMES_HOME_DIR/plugins/$name" ]; then
    printf '%s\n' "Kiro component '$name' is already installed."
  else
    hermes plugins install "$source" --no-enable
  fi
}

install_if_missing kiro "$SOURCE/commands"
install_if_missing kiro-provider "$SOURCE/provider"

# Enable only after both component manifests are present and discoverable.
hermes plugins doctor "$HERMES_HOME_DIR/plugins/kiro" --ci
hermes plugins doctor "$HERMES_HOME_DIR/plugins/kiro-provider" --ci
hermes plugins enable kiro

cat <<'EOF'

Kiro for Hermes is installed.

Start a new terminal session, then authenticate:
  hermes kiro login

If the Hermes gateway is already running, restart it once so it loads the provider:
  systemctl --user restart hermes-gateway

The installer did not restart the gateway.
EOF
