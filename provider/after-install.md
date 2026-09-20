# Kiro provider installed

Authenticate through Hermes' native provider-auth surface:

```bash
hermes auth add kiro
```

Then use `hermes auth status|list|refresh|logout kiro`. Select Kiro in the normal main or auxiliary model picker; `/usage` shows Kiro allowances when it is active.

Restart an already-running gateway once so it imports the new provider code:

```bash
systemctl --user restart hermes-gateway
```
