"""Hermes-owned IAM Identity Center credentials for Kiro."""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Lock
from typing import Any

from hermes_constants import get_hermes_home
from transport import KiroHTTPError, request_json

_API_REGIONS = {"us-east-1", "eu-central-1"}
_REGION_MAP = {
    "us-west-1": "us-east-1", "us-west-2": "us-east-1", "us-east-2": "us-east-1",
    "ap-southeast-1": "us-east-1", "ap-southeast-2": "us-east-1", "ap-northeast-1": "us-east-1", "ap-south-1": "us-east-1",
    "eu-west-1": "eu-central-1", "eu-west-2": "eu-central-1", "eu-west-3": "eu-central-1", "eu-north-1": "eu-central-1", "eu-south-1": "eu-central-1", "eu-south-2": "eu-central-1", "eu-central-2": "eu-central-1",
}
_SCOPES = ["codewhisperer:completions", "codewhisperer:analysis", "codewhisperer:conversations"]
_GRANTS = ["urn:ietf:params:oauth:grant-type:device_code", "refresh_token"]
BUILDER_ID_START_URL = "https://view.awsapps.com/start"
_LOCK = Lock()
_CACHED: dict[Path, "Credentials"] = {}


class KiroAuthError(RuntimeError):
    pass


@dataclass
class Credentials:
    access_token: str
    refresh_token: str
    client_id: str
    client_secret: str
    region: str
    start_url: str
    expires_at: float
    client_secret_expires_at: float = 0.0
    profile_arn: str = ""

    @property
    def api_region(self) -> str:
        return runtime_region(self.region)

    @property
    def expiring(self) -> bool:
        return time.time() >= self.expires_at - 300

    @property
    def is_builder_id(self) -> bool:
        return self.start_url.rstrip("/") == BUILDER_ID_START_URL.rstrip("/")


def hermes_home() -> Path:
    """Use Hermes' context-local profile home, not only process environment."""
    return get_hermes_home()


def credential_path() -> Path:
    return hermes_home() / "kiro" / "credentials.json"


def _cached() -> Credentials | None:
    return _CACHED.get(credential_path())


def _cache(creds: Credentials) -> None:
    _CACHED[credential_path()] = creds


def runtime_region(region: str) -> str:
    region = (region or "").strip().lower()
    if region in _API_REGIONS:
        return region
    if region in _REGION_MAP:
        return _REGION_MAP[region]
    raise KiroAuthError(f"Unsupported IAM Identity Center region: {region!r}")


def validate_start_url(value: str) -> str:
    parsed = urllib.parse.urlparse((value or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port or (host != "awsapps.com" and not host.endswith(".awsapps.com")):
        raise KiroAuthError("start URL must be an HTTPS awsapps.com IAM Identity Center URL")
    if parsed.query or parsed.fragment:
        raise KiroAuthError("start URL must not include a query string or fragment")
    return urllib.parse.urlunparse(("https", parsed.netloc, parsed.path.rstrip("/") or "/start", "", "", ""))


def _endpoint(region: str, path: str) -> str:
    runtime_region(region)  # validate before the credential-bearing URL is built
    return f"https://oidc.{region}.amazonaws.com/{path}"


def _post(region: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return request_json(
            "POST",
            _endpoint(region, path),
            body=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
    except KiroHTTPError as exc:
        raise KiroAuthError(f"AWS OIDC {path} failed ({exc.status}): {exc.body.decode('utf-8', 'replace')[:500]}") from exc
    except Exception as exc:
        raise KiroAuthError(f"AWS OIDC {path} is unreachable: {exc}") from exc


def _write(creds: Credentials) -> None:
    target = credential_path()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="credentials.", dir=target.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(asdict(creds), handle)
            handle.write("\n")
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def save_credentials(creds: Credentials) -> None:
    """Persist a token or IdC profile update made by the native transport."""
    global _CACHED
    _cache(creds)
    _write(creds)


def _read() -> Credentials:
    try:
        data = json.loads(credential_path().read_text())
        return Credentials(**data)
    except (OSError, TypeError, ValueError) as exc:
        raise KiroAuthError("Kiro is not logged in. Run credentials.py login with your IAM Identity Center start URL.") from exc


def _enable_provider() -> None:
    env = hermes_home() / ".env"
    line = "KIRO_AUTH=kiro-oauth-local"
    existing = env.read_text() if env.exists() else ""
    if not any(row.startswith("KIRO_AUTH=") for row in existing.splitlines()):
        with env.open("a", encoding="utf-8") as handle:
            if existing and not existing.endswith("\n"):
                handle.write("\n")
            handle.write(line + "\n")


def _disable_provider() -> None:
    env = hermes_home() / ".env"
    if not env.exists():
        return
    kept = [row for row in env.read_text().splitlines() if row != "KIRO_AUTH=kiro-oauth-local"]
    env.write_text("\n".join(kept) + ("\n" if kept else ""))


def logout() -> bool:
    """Forget only Hermes' native Kiro credentials and sentinel."""
    global _CACHED
    with _LOCK:
        target = credential_path()
        existed = target.exists()
        target.unlink(missing_ok=True)
        _CACHED.pop(target, None)
        _disable_provider()
        return existed


def login(start_url: str = BUILDER_ID_START_URL, region: str = "us-east-1") -> tuple[str, str]:
    """Run an IdC device-code login; returns the verification URL and user code."""
    global _CACHED
    start_url = validate_start_url(start_url)
    runtime_region(region)
    registration = _post(region, "client/register", {"clientName": "hermes-kiro", "clientType": "public", "scopes": _SCOPES, "grantTypes": _GRANTS, "issuerUrl": start_url})
    client_id, client_secret = registration.get("clientId"), registration.get("clientSecret")
    if not client_id or not client_secret:
        raise KiroAuthError("AWS OIDC did not return a client registration")
    device = _post(region, "device_authorization", {"clientId": client_id, "clientSecret": client_secret, "startUrl": start_url})
    code, user_code, uri = device.get("deviceCode"), device.get("userCode"), device.get("verificationUriComplete") or device.get("verificationUri")
    if not code or not user_code or not uri:
        raise KiroAuthError("AWS OIDC did not return device authorization details")
    print(f"Open: {uri}\nCode: {user_code}", flush=True)
    deadline = time.monotonic() + float(device.get("expiresIn") or 600)
    interval = max(1.0, float(device.get("interval") or 5))
    while time.monotonic() < deadline:
        try:
            token = _post(region, "token", {"clientId": client_id, "clientSecret": client_secret, "grantType": "urn:ietf:params:oauth:grant-type:device_code", "deviceCode": code})
        except KiroAuthError as exc:
            text = str(exc)
            if "authorization_pending" in text:
                time.sleep(interval)
                continue
            if "slow_down" in text:
                interval += 2
                time.sleep(interval)
                continue
            raise
        access, refresh = token.get("accessToken"), token.get("refreshToken")
        if not access or not refresh:
            raise KiroAuthError("AWS OIDC token response omitted access or refresh token")
        save_credentials(Credentials(access, refresh, client_id, client_secret, region, start_url, time.time() + float(token.get("expiresIn") or 3600), float(registration.get("clientSecretExpiresAt") or 0)))
        _enable_provider()
        return str(uri), str(user_code)
    raise KiroAuthError("Device authorization expired; run login again")


def _next_login_choice(selected: int, key: str) -> int:
    if key == "up":
        return max(0, selected - 1)
    if key == "down":
        return min(1, selected + 1)
    return selected


def _arrow_login_choice() -> str | None:
    """Return a keyboard-selected method, or None when stdin is not a terminal."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return None
    try:
        import termios
        import tty
    except ImportError:
        return None
    selected = 0
    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)

    def draw() -> None:
        options = ("AWS Builder ID", "IAM Identity Center")
        sys.stdout.write("\x1b[2J\x1b[H\nKiro login\n\nSelect login method:\n\n")
        for index, option in enumerate(options):
            sys.stdout.write(f"  {'❯' if index == selected else ' '} {option}\n")
        sys.stdout.write("\nUse ↑ / ↓ to choose, then Enter.\n")
        sys.stdout.flush()

    try:
        tty.setcbreak(fd)
        sys.stdout.write("\x1b[?25l")
        draw()
        while True:
            key = sys.stdin.read(1)
            if key in {"\r", "\n"}:
                return str(selected + 1)
            if key == "\x03":
                raise KeyboardInterrupt
            if key == "\x1b" and sys.stdin.read(1) == "[":
                key = sys.stdin.read(1)
                selected = _next_login_choice(selected, "up" if key == "A" else "down" if key == "B" else "")
                draw()
            elif key in {"1", "2"}:
                selected = int(key) - 1
                draw()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)
        sys.stdout.write("\x1b[?25h\n")
        sys.stdout.flush()


def prompt_login_inputs(start_url: str | None, region: str | None, input_fn=input) -> tuple[str, str]:
    """Resolve CLI flags interactively while preserving scriptable flags."""
    if start_url is None:
        choice = _arrow_login_choice() or input_fn("\nSelect login method:\n\n  1. AWS Builder ID\n  2. IAM Identity Center\n\nChoice [1]: ").strip()
        if choice not in {"", "1", "2"}:
            raise KiroAuthError("Choose 1 for AWS Builder ID or 2 for IAM Identity Center")
        start_url = BUILDER_ID_START_URL if choice in {"", "1"} else input_fn("\nIAM Identity Center start URL:\n\n> ").strip()
    if region is None:
        region = input_fn("\nIAM Identity Center region [us-east-1]:\n\n> ").strip() or "us-east-1"
    return start_url, region


def get_credentials(force_refresh: bool = False, stale_access_token: str = "") -> Credentials:
    """Return a valid token; concurrent stale requests refresh it once."""
    global _CACHED
    with _LOCK:
        creds = _cached() or _read()
        if creds.client_secret_expires_at and time.time() >= creds.client_secret_expires_at:
            raise KiroAuthError("Kiro client registration expired; run login again")
        if force_refresh and stale_access_token and creds.access_token != stale_access_token:
            return creds
        if not force_refresh and not creds.expiring:
            _cache(creds)
            return creds
        token = _post(creds.region, "token", {"clientId": creds.client_id, "clientSecret": creds.client_secret, "grantType": "refresh_token", "refreshToken": creds.refresh_token})
        access = token.get("accessToken")
        if not access:
            raise KiroAuthError("Kiro token refresh returned no access token; run login again")
        creds.access_token = access
        creds.refresh_token = token.get("refreshToken") or creds.refresh_token
        creds.expires_at = time.time() + float(token.get("expiresIn") or 3600)
        save_credentials(creds)
        return creds


def main() -> None:
    parser = argparse.ArgumentParser(description="Hermes-native Kiro IdC login")
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("login")
    command.add_argument("--start-url", help="IAM Identity Center URL; omit for interactive setup")
    command.add_argument("--region", help="IAM Identity Center region; omit for interactive setup")
    sub.add_parser("status")
    args = parser.parse_args()
    if args.command == "login":
        login(*prompt_login_inputs(args.start_url, args.region))
        print("Kiro login saved. Restart Hermes, then select provider kiro.")
    else:
        creds = get_credentials()
        print(f"logged in; IdC region={creds.region}; runtime region={creds.api_region}; expires_in={int(creds.expires_at-time.time())}s")


if __name__ == "__main__":
    main()
