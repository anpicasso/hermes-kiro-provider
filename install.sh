#!/usr/bin/env bash
# Install the Hermes Kiro plugin (provider + its built-in commands). No gateway restart is performed.
set -euo pipefail

SOURCE="anpicasso/hermes-kiro-provider"
HERMES_ROOT="${HERMES_HOME:-$HOME/.hermes}"
HERMES_HOME_DIR="$HERMES_ROOT"
HERMES_ARGS=()
if [ -n "${HERMES_PROFILE:-}" ]; then
  profile="$HERMES_PROFILE"
  if ! printf '%s' "$profile" | grep -Eq '^(default|[a-z0-9][a-z0-9_-]{0,63})$'; then
    printf '%s\n' "Invalid Hermes profile: $profile" >&2
    exit 1
  fi
  HERMES_ARGS=(--profile "$profile")
  if [ "$profile" != "default" ]; then
    HERMES_HOME_DIR="$HERMES_ROOT/profiles/$profile"
  fi
# Match Hermes: an explicit profile directory wins; otherwise use active_profile.
elif { [ -z "${HERMES_HOME:-}" ] || [ "$(basename "$(dirname "$HERMES_HOME")")" != "profiles" ]; } && [ -f "$HERMES_ROOT/active_profile" ]; then
  profile="$(tr -d '\r\n' < "$HERMES_ROOT/active_profile")"
  if ! printf '%s' "$profile" | grep -Eq '^(default|[a-z0-9][a-z0-9_-]{0,63})$'; then
    printf '%s\n' "Invalid active Hermes profile: $profile" >&2
    exit 1
  fi
  if [ "$profile" != "default" ]; then
    HERMES_HOME_DIR="$HERMES_ROOT/profiles/$profile"
  fi
fi
export HERMES_HOME="$HERMES_HOME_DIR"

if ! command -v hermes >/dev/null 2>&1; then
  printf '%s\n' 'Hermes CLI was not found in PATH. Install Hermes first.' >&2
  exit 1
fi
if ! command -v git >/dev/null 2>&1; then
  printf '%s\n' 'git was not found in PATH. It is required to resolve the immutable plugin revision.' >&2
  exit 1
fi
ref_line="$(git ls-remote "https://github.com/${SOURCE}.git" refs/heads/main)" || {
  printf '%s\n' 'Could not resolve the current Kiro plugin revision.' >&2
  exit 1
}
REF="${ref_line%%$'\t'*}"
if [[ ! "$REF" =~ ^[0-9a-f]{40}$ ]]; then
  printf '%s\n' 'Kiro plugin revision was not a valid commit SHA.' >&2
  exit 1
fi

# Hermes installs portable snapshots, so `plugins update` cannot update them.
hermes "${HERMES_ARGS[@]}" plugins install "$SOURCE/provider" --ref "$REF" --force --no-enable

# Remove a leftover companion plugin from earlier releases (now merged into provider/).
if [ -d "$HERMES_HOME_DIR/plugins/kiro" ]; then
  printf '%s\n' 'Removing the obsolete `kiro` companion plugin (merged into kiro-provider).'
  rm -rf "$HERMES_HOME_DIR/plugins/kiro"
fi

hermes "${HERMES_ARGS[@]}" plugins doctor "$HERMES_HOME_DIR/plugins/kiro-provider" --ci

# Remove any legacy plugins.enabled entry for the old companion.
hermes "${HERMES_ARGS[@]}" config get plugins.enabled 2>/dev/null | grep -q 'kiro$\|kiro"' 2>/dev/null && {
  printf '%s\n' 'Note: `plugins/kiro` was removed from this install. It was merged into kiro-provider.'
} || true

cat <<'EOF'

Kiro for Hermes is installed.

Start a new terminal session, then authenticate:
  hermes kiro login

If the Hermes gateway is already running, restart it once so it loads the provider:
  systemctl --user restart hermes-gateway

The installer did not restart the gateway.
EOF
