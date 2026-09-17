# Kiro provider installed

Sign in before selecting the provider:

```bash
hermes kiro login
```

Then `hermes kiro status`, `hermes kiro usage`, and `/kiro status` / `/kiro usage` in a session.

If `hermes kiro` is unavailable after a Hermes upgrade, the same commands run standalone:

```bash
python ~/.hermes/plugins/kiro-provider/commands.py {login|status|usage|logout}
```
