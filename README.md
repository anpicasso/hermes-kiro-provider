# hermes-kiro

Native Kiro model-provider plugin for Hermes. It talks directly to Kiro over HTTPS: no `kiro-cli`, no loopback proxy, no gateway.

## v0.1 scope

- AWS Builder ID device authorization by default; optionally accepts an IAM Identity Center `start URL`.
- Hermes-owned credentials in `$HERMES_HOME/kiro/credentials.json` (mode `0600`).
- Refresh-token rotation, direct Kiro event-stream transport, system/user translation, and basic tool-result turns.
- Social login and account quota are deliberately not in this first cut.

## Install locally

```bash
mkdir -p ~/.hermes/plugins
cp -R commands ~/.hermes/plugins/kiro
cp -R provider ~/.hermes/plugins/kiro-provider
hermes plugins enable kiro
hermes kiro login                         # AWS Builder ID
hermes kiro login --start-url 'https://YOUR-START-URL.awsapps.com/start' --region us-east-1  # corporate IdC
# Restart Hermes, then choose provider `kiro`.
```

The login command stores only Kiro's IdC registration and tokens. It adds `KIRO_AUTH=kiro-oauth-local` to Hermes' `.env`; that is a sentinel for Hermes' provider registry, not a credential.

## Development

```bash
PYTHONPATH=provider ~/.hermes/hermes-agent/venv/bin/python -m pytest tests -q
```

Kiro is an undocumented private API. Use your own entitlement; never commit credentials.
