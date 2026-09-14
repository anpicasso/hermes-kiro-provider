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


def _kiro():
    directory = _provider_dir()
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
    from client import format_usage, get_usage_limits
    from credentials import get_credentials, login, logout, prompt_login_inputs
    return get_credentials, login, logout, prompt_login_inputs, get_usage_limits, format_usage


def setup_kiro_parser(parser) -> None:
    sub = parser.add_subparsers(dest="kiro_command", required=True)
    login_parser = sub.add_parser("login", help="Sign in to Kiro via IAM Identity Center")
    login_parser.add_argument("--start-url", help="IAM Identity Center URL; omit for interactive setup")
    login_parser.add_argument("--region", help="IAM Identity Center region; omit for interactive setup")
    sub.add_parser("status", help="Show Kiro login status")
    sub.add_parser("usage", help="Show Kiro usage allowances")
    logout_parser = sub.add_parser("logout", help="Forget Hermes' native Kiro login")
    logout_parser.add_argument("--yes", action="store_true", help="Do not ask for confirmation")


def handle_kiro(args) -> None:
    get_credentials, login, logout, prompt_login_inputs, get_usage_limits, format_usage = _kiro()
    if args.kiro_command == "login":
        login(*prompt_login_inputs(args.start_url, args.region))
        print("Kiro login saved. New CLI chats can use it immediately; restart only a running Hermes gateway.")
        return
    if args.kiro_command == "usage":
        print(format_usage(get_usage_limits()))
        return
    if args.kiro_command == "logout":
        if not args.yes and (not sys.stdin.isatty() or input("Forget Hermes' Kiro credentials? [y/N] ").strip().lower() not in {"y", "yes"}):
            print("Kiro logout cancelled.")
            return
        print("Kiro credentials removed." if logout() else "Kiro was already logged out.")
        return
    creds = get_credentials()
    import time
    print(f"logged in; IdC region={creds.region}; runtime region={creds.api_region}; expires_in={int(creds.expires_at - time.time())}s")


def handle_kiro_slash(raw_args: str) -> str:
    parts = shlex.split(raw_args or "")
    if parts[:1] == ["status"]:
        try:
            get_credentials, *_ = _kiro()
            creds = get_credentials()
            import time
            return f"Kiro logged in; IdC region={creds.region}; expires in {int(creds.expires_at - time.time())}s."
        except Exception as exc:
            return f"Kiro is not logged in: {exc}"
    if parts[:1] == ["usage"]:
        try:
            *_, get_usage_limits, format_usage = _kiro()
            return format_usage(get_usage_limits())
        except Exception as exc:
            return f"Kiro usage failed: {exc}"
    if parts[:1] == ["logout"]:
        return "Logout changes local credentials; run `hermes kiro logout` in a terminal."
    return "Use `hermes kiro login` for AWS Builder ID or IAM Identity Center. `/kiro status` and `/kiro usage` are available here."


def register(ctx) -> None:
    ctx.register_cli_command(name="kiro", help="Kiro login, usage and status", setup_fn=setup_kiro_parser, handler_fn=handle_kiro, description="Kiro IAM Identity Center device-code login.")
    ctx.register_command(name="kiro", handler=handle_kiro_slash, description="Kiro status and usage", args_hint="status|usage", argument_mode="text")
