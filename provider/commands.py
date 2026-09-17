"""`hermes kiro` / `/kiro` command surface, owned by the provider plugin.

Hermes skips ``kind: model-provider`` manifests in the command-plugin loader, so the provider's
``register(ctx)`` is never called with a real context (see #111258). Registration therefore builds
its own PluginContext in ``__init__.py``; that touches internal API, so it is wrapped and this
module stays runnable on its own:

    python ~/.hermes/plugins/kiro-provider/commands.py {login|status|usage|logout}

That fallback imports no Hermes plugin code at all, so it survives any change to the plugin API.
"""
from __future__ import annotations

import shlex
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:  # direct `python commands.py` run
    sys.path.insert(0, str(_HERE))

FALLBACK_HINT = (
    f"  python {_HERE / 'commands.py'} " + "{login|status|usage|logout}"
)


def _status_line() -> str:
    from credentials import get_credentials
    creds = get_credentials()
    return (f"Kiro logged in; IdC region={creds.region}; runtime region={creds.api_region}; "
            f"expires_in={int(creds.expires_at - time.time())}s")


# --- argparse surface for `hermes kiro` -------------------------------------------------

def setup_parser(parser) -> None:
    sub = parser.add_subparsers(dest="kiro_command", required=True)
    login_parser = sub.add_parser("login", help="Sign in to Kiro via IAM Identity Center")
    login_parser.add_argument("--start-url", help="IAM Identity Center URL; omit for interactive setup")
    login_parser.add_argument("--region", help="IAM Identity Center region; omit for interactive setup")
    sub.add_parser("status", help="Show Kiro login status")
    sub.add_parser("usage", help="Show Kiro usage allowances")
    logout_parser = sub.add_parser("logout", help="Forget Hermes' native Kiro login")
    logout_parser.add_argument("--yes", action="store_true", help="Do not ask for confirmation")


def handle(args) -> None:
    command = getattr(args, "kiro_command", "status")
    if command == "login":
        from credentials import login, prompt_login_inputs
        login(*prompt_login_inputs(args.start_url, args.region))
        print("Kiro login saved. New CLI chats can use it immediately; restart only a running Hermes gateway.")
        return
    if command == "usage":
        from client import format_usage, get_usage_limits
        print(format_usage(get_usage_limits()))
        return
    if command == "logout":
        from credentials import logout
        confirmed = getattr(args, "yes", False) or (
            sys.stdin.isatty()
            and input("Forget Hermes' Kiro credentials? [y/N] ").strip().lower() in {"y", "yes"})
        if not confirmed:
            print("Kiro logout cancelled.")
            return
        print("Kiro credentials removed." if logout() else "Kiro was already logged out.")
        return
    print(_status_line())


# --- in-session `/kiro` -----------------------------------------------------------------

def handle_slash(raw_args: str) -> str:
    try:
        parts = shlex.split(raw_args or "")
    except ValueError as exc:
        return f"Invalid Kiro command: {exc}"
    action = parts[0] if parts else ""
    if action == "status":
        try:
            return _status_line() + "."
        except Exception as exc:
            return f"Kiro is not logged in: {exc}"
    if action == "usage":
        try:
            from client import format_usage, get_usage_limits
            return format_usage(get_usage_limits())
        except Exception as exc:
            return f"Kiro usage failed: {exc}"
    if action == "logout":
        return "Logout changes local credentials; run `hermes kiro logout` in a terminal."
    return ("Use `hermes kiro login` for AWS Builder ID or IAM Identity Center. "
            "`/kiro status` and `/kiro usage` are available here.")


# --- rescue entrypoint: no Hermes plugin API imports, works when registration fails -------

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(
        prog="commands.py", description="Kiro login/status/usage/logout without the Hermes CLI")
    setup_parser(parser)
    handle(parser.parse_args())


if __name__ == "__main__":
    main()
