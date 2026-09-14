# hermes-plugin-kiro

Native Kiro model-provider plugin for Hermes. It connects Hermes directly to Kiro over HTTPS: no local HTTP listener, proxy, daemon, or `kiro-cli` is required.

## Why native HTTPS

- **Direct transport:** Hermes streams Kiro responses over one shared HTTPS connection pool, including concurrent conversations.
- **No local service:** nothing listens on a loopback port, no helper binary is downloaded, and no daemon has to be supervised.
- **Native Kiro auth:** AWS Builder ID or IAM Identity Center device authorization stores only the Kiro OIDC registration and tokens under `$HERMES_HOME/kiro/credentials.json` (directory `0700`, file `0600`). Credentials and refreshes follow the active Hermes profile. Tokens refresh automatically.
- **Kiro-aware runtime:** live model catalog, Kiro event-stream framing, tool calls/results, and Kiro usage limits are handled by the provider client.

## Architecture

```mermaid
flowchart LR
    U[User] --> C[commands plugin\nhermes kiro login / logout / usage]
    C --> S[$HERMES_HOME/kiro/credentials.json]
    H[Hermes CLI / Gateway] --> P[kiro-provider\ncustom Hermes client]
    P --> S
    P --> T[Shared HTTPS pool]
    T --> K[Kiro HTTPS APIs\nstream · models · usage]
```

## Why two plugins

Hermes discovers model providers and command plugins through separate native extension paths:

- **`provider/`** is `kind: model-provider`. It registers Kiro for `/model`, the model picker, CLI, gateway, and auxiliary calls.
- **`commands/`** is `kind: standalone`. It owns the interactive terminal login and the `hermes kiro` / `/kiro` command surfaces.

They share one credential store and one provider; the split exists only because a model-provider plugin is deliberately not loaded by Hermes' command-plugin manager.

The components are deliberately **not usable independently**. The command component returns an install instruction until `kiro-provider` exists; the provider returns the reciprocal instruction until `kiro` exists. The one-line installer installs, validates, then enables the command component only after both are present.

### Why login needs its own plugin today

The model-provider package is loaded for provider discovery, but Hermes does not run its command-registration hook and does not expose a plugin authentication callback for device authorization. The standalone package supplies that missing command surface while the provider continues to own the credentials and HTTPS client. [Upstream feature request #111258](https://github.com/NousResearch/hermes-agent/issues/111258) proposes a native provider-auth hook so a future version can remove this companion plugin.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/anpicasso/hermes-plugin-kiro/main/install.sh | bash
```

The installer installs or updates both plugin directories and enables the command plugin **for one Hermes profile**. It uses the active profile (`hermes profile use <name>`); if `HERMES_HOME` is set, that explicit profile home wins. It **does not restart anything**.

### Profiles

Plugins live under each profile's `$HERMES_HOME/plugins`, so install them once in every profile that will use Kiro. Each profile then has its own `$HERMES_HOME/kiro/credentials.json`; logging into one never shares its Kiro account with another.

```bash
# Install and log in for the active profile.
hermes profile use kaito
curl -fsSL https://raw.githubusercontent.com/anpicasso/hermes-plugin-kiro/main/install.sh | bash
hermes kiro login

# Or target a profile explicitly without changing the active profile.
curl -fsSL https://raw.githubusercontent.com/anpicasso/hermes-plugin-kiro/main/install.sh | HERMES_HOME="$HOME/.hermes/profiles/kaito" bash
hermes kiro login --profile kaito
```

- A new terminal can immediately run `hermes kiro login`.
- Restart an already-running Hermes gateway once so it imports the newly installed provider:

```bash
systemctl --user restart hermes-gateway
```

Then authenticate:

```bash
hermes kiro login
```

Choose AWS Builder ID or corporate IAM Identity Center with the arrow keys. Flags remain available for automation:

```bash
hermes kiro login --start-url 'https://YOUR-START-URL.awsapps.com/start' --region us-east-1
hermes kiro usage
hermes kiro logout
```

## Manual install

```bash
hermes plugins install anpicasso/hermes-plugin-kiro/commands --no-enable
hermes plugins install anpicasso/hermes-plugin-kiro/provider --no-enable
hermes plugins enable kiro
```

Installing either subdirectory directly is safe, but it remains unusable until its companion is installed. Hermes currently has no third-party post-install hook, so only the one-line installer can automatically enable `kiro` after confirming both components.

`logout` removes only this plugin's local credentials. It does not invent a remote revoke endpoint.

## Development

```bash
PYTHONPATH=provider ~/.hermes/hermes-agent/venv/bin/python -m pytest tests -q
hermes plugins doctor commands --ci
hermes plugins doctor provider --ci
```

Kiro is an undocumented private API. Use your own entitlement; never commit credentials.
