"""`hermes kiro` and `/kiro` commands; credentials remain owned by the provider."""
from __future__ import annotations

import shlex
import sys
from pathlib import Path


def _provider_dir() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return get_hermes_home() / "plugins" / "kiro-provider"
    except Exception:
        return Path(__file__).resolve().parents[1] / "provider"


def _credentials():
    directory = _provider_dir()
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
    from credentials import get_credentials, login
    return get_credentials, login


def setup_kiro_parser(parser) -> None:
    sub = parser.add_subparsers(dest="kiro_command", required=True)
    login_parser = sub.add_parser("login", help="Sign in to Kiro via IAM Identity Center")
    login_parser.add_argument("--start-url", required=True)
    login_parser.add_argument("--region", default="us-east-1")
    sub.add_parser("status", help="Show Kiro login status")


def handle_kiro(args) -> None:
    get_credentials, login = _credentials()
    if args.kiro_command == "login":
        login(args.start_url, args.region)
        print("Kiro login saved. Restart Hermes, then select provider kiro.")
        return
    creds = get_credentials()
    import time
    print(f"logged in; IdC region={creds.region}; runtime region={creds.api_region}; expires_in={int(creds.expires_at - time.time())}s")


def handle_kiro_slash(raw_args: str) -> str:
    parts = shlex.split(raw_args or "")
    if parts[:1] == ["status"]:
        try:
            get_credentials, _ = _credentials()
            creds = get_credentials()
            import time
            return f"Kiro logged in; IdC region={creds.region}; expires in {int(creds.expires_at - time.time())}s."
        except Exception as exc:
            return f"Kiro is not logged in: {exc}"
    return "Use `hermes kiro login --start-url https://YOUR.awsapps.com/start --region us-east-1` in a terminal. Device authorization needs that terminal to stay open; `/kiro status` works here."


def register(ctx) -> None:
    ctx.register_cli_command(name="kiro", help="Kiro login and status", setup_fn=setup_kiro_parser, handler_fn=handle_kiro, description="Kiro IAM Identity Center device-code login.")
    ctx.register_command(name="kiro", handler=handle_kiro_slash, description="Kiro login and status", args_hint="status", argument_mode="text")
