# hermes-kiro

Native Kiro model-provider plugin for Hermes. It talks directly to Kiro over HTTPS: no `kiro-cli`, no loopback proxy, no gateway.

## v0.1 scope

- AWS Builder ID device authorization by default; optionally accepts an IAM Identity Center `start URL`.
- Hermes-owned credentials in `$HERMES_HOME/kiro/credentials.json` (mode `0600`).
- Automatic single-flight refresh-token rotation and a process-wide HTTP connection pool for concurrent Hermes conversations.
- Direct Kiro event-stream transport, dynamic model catalog, system/user translation, and tool-result turns.
- `hermes kiro usage` / `/kiro usage`; `hermes kiro logout` removes only this plugin's local credentials.
- Social login is deliberately out of scope.

## Install locally

```bash
hermes plugins install anpicasso/hermes-kiro/commands --no-enable
hermes plugins install anpicasso/hermes-kiro/provider --no-enable
hermes plugins enable kiro
hermes kiro login                         # asks Builder ID vs corporate IdC, then inputs
hermes kiro login --start-url 'https://YOUR-START-URL.awsapps.com/start' --region us-east-1  # corporate IdC
hermes kiro usage                         # current Kiro allowances
hermes kiro logout                        # removes only ~/.hermes/kiro credentials
# New CLI processes see login immediately; restart only an already-running Hermes gateway.
```

The login command stores only Kiro's IdC registration and tokens. It adds `KIRO_AUTH=kiro-oauth-local` to Hermes' `.env`; that is a sentinel for Hermes' provider registry, not a credential.

## Development

```bash
PYTHONPATH=provider ~/.hermes/hermes-agent/venv/bin/python -m pytest tests -q
```

Kiro is an undocumented private API. Use your own entitlement; never commit credentials.
