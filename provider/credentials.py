"""Kiro IAM Identity Center auth backed by Hermes' credential pool."""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hermes_constants import get_hermes_home
from transport import KiroHTTPError, request_json

if TYPE_CHECKING:
    from agent.credential_pool import PooledCredential

PROVIDER = "kiro"
SOURCE = "manual:kiro_device_code"
RUNTIME_BASE_URL = "https://runtime.us-east-1.kiro.dev"
_API_REGIONS = {"us-east-1", "eu-central-1"}
_REGION_MAP = {
    "us-west-1": "us-east-1", "us-west-2": "us-east-1", "us-east-2": "us-east-1",
    "ap-southeast-1": "us-east-1", "ap-southeast-2": "us-east-1", "ap-northeast-1": "us-east-1", "ap-south-1": "us-east-1",
    "eu-west-1": "eu-central-1", "eu-west-2": "eu-central-1", "eu-west-3": "eu-central-1", "eu-north-1": "eu-central-1", "eu-south-1": "eu-central-1", "eu-south-2": "eu-central-1", "eu-central-2": "eu-central-1",
}
_SCOPES = ["codewhisperer:completions", "codewhisperer:analysis", "codewhisperer:conversations"]
_GRANTS = ["urn:ietf:params:oauth:grant-type:device_code", "refresh_token"]
_TERMINAL_REFRESH_CODES = {"invalid_client", "invalid_grant", "invalid_token", "unauthorized_client"}
BUILDER_ID_START_URL = "https://view.awsapps.com/start"
_PROFILE_ARNS: dict[str, str] = {}


class KiroAuthError(RuntimeError):
    def __init__(self, message: str, *, code: str = "", status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


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
    credential_id: str = ""

    @property
    def api_region(self) -> str:
        return runtime_region(self.region)

    @property
    def is_builder_id(self) -> bool:
        return self.start_url.rstrip("/") == BUILDER_ID_START_URL.rstrip("/")


def hermes_home() -> Path:
    """Use Hermes' context-local profile home, not only process environment."""
    return get_hermes_home()


def state_dir() -> Path:
    """Return Kiro's profile-scoped non-secret state directory."""
    return hermes_home() / "kiro"


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
    runtime_region(region)
    return f"https://oidc.{region}.amazonaws.com/{path}"


def _error_code(body: bytes) -> str:
    try:
        data = json.loads(body or b"{}")
    except (TypeError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("error") or data.get("code") or data.get("reason") or "").strip().lower()


def _post(region: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return request_json(
            "POST",
            _endpoint(region, path),
            body=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
    except KiroHTTPError as exc:
        raise KiroAuthError(
            f"AWS OIDC {path} failed ({exc.status}): {exc.body.decode('utf-8', 'replace')[:500]}",
            code=_error_code(exc.body), status=exc.status,
        ) from exc
    except Exception as exc:
        if isinstance(exc, KiroAuthError):
            raise
        raise KiroAuthError(f"AWS OIDC {path} is unreachable: {exc}") from exc


def _entry_credentials(entry: PooledCredential) -> Credentials:
    extra = entry.extra or {}
    required = ("client_id", "client_secret", "region", "start_url")
    missing = [key for key in required if not str(extra.get(key) or "").strip()]
    if missing:
        raise KiroAuthError(
            "Kiro credential metadata is incomplete "
            f"({', '.join(missing)}); run `hermes auth add kiro` again."
        )
    expires_at = float(entry.expires_at_ms or 0) / 1000.0
    credential_id = entry.id
    return Credentials(
        access_token=entry.access_token,
        refresh_token=entry.refresh_token or "",
        client_id=str(extra["client_id"]),
        client_secret=str(extra["client_secret"]),
        region=str(extra["region"]),
        start_url=str(extra["start_url"]),
        expires_at=expires_at,
        client_secret_expires_at=float(extra.get("client_secret_expires_at") or 0),
        profile_arn=str(_PROFILE_ARNS.get(credential_id) or ""),
        credential_id=credential_id,
    )


def _pool_entry(creds: Credentials, *, label: str, source: str = SOURCE) -> PooledCredential:
    from agent.credential_pool import AUTH_TYPE_OAUTH, PooledCredential

    return PooledCredential(
        provider=PROVIDER,
        id=uuid.uuid4().hex[:6],
        label=label,
        auth_type=AUTH_TYPE_OAUTH,
        priority=0,
        source=source,
        base_url=RUNTIME_BASE_URL,
        access_token=creds.access_token,
        refresh_token=creds.refresh_token,
        expires_at_ms=int(creds.expires_at * 1000),
        extra={
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "region": creds.region,
            "start_url": creds.start_url,
            "client_secret_expires_at": creds.client_secret_expires_at,
        },
    )


def persist_credentials(
    creds: Credentials, *, label: str | None = None, priority: int | None = None, source: str = SOURCE
) -> PooledCredential:
    from agent.credential_pool import load_pool

    pool = load_pool(PROVIDER)
    label = (label or "").strip() or f"kiro-oauth-{len(pool.entries()) + 1}"
    entry = pool.add_entry(_pool_entry(creds, label=label, source=source))
    if priority is not None:
        entry = pool.move_entry(entry.id, int(priority)) or entry
    return entry


def remember_profile_arn(creds: Credentials) -> None:
    """Cache discovered non-secret profile metadata without racing token rotation on disk."""
    if creds.credential_id and creds.profile_arn:
        _PROFILE_ARNS[creds.credential_id] = creds.profile_arn


def _device_login(start_url: str, region: str) -> tuple[Credentials, str, str]:
    start_url = validate_start_url(start_url)
    runtime_region(region)
    registration = _post(region, "client/register", {
        "clientName": "hermes-kiro", "clientType": "public", "scopes": _SCOPES,
        "grantTypes": _GRANTS, "issuerUrl": start_url,
    })
    client_id, client_secret = registration.get("clientId"), registration.get("clientSecret")
    if not client_id or not client_secret:
        raise KiroAuthError("AWS OIDC did not return a client registration")
    device = _post(region, "device_authorization", {
        "clientId": client_id, "clientSecret": client_secret, "startUrl": start_url,
    })
    code, user_code = device.get("deviceCode"), device.get("userCode")
    uri = device.get("verificationUriComplete") or device.get("verificationUri")
    if not code or not user_code or not uri:
        raise KiroAuthError("AWS OIDC did not return device authorization details")
    print(f"Open: {uri}\nCode: {user_code}", flush=True)
    deadline = time.monotonic() + float(device.get("expiresIn") or 600)
    interval = max(1.0, float(device.get("interval") or 5))
    while time.monotonic() < deadline:
        try:
            token = _post(region, "token", {
                "clientId": client_id, "clientSecret": client_secret,
                "grantType": "urn:ietf:params:oauth:grant-type:device_code", "deviceCode": code,
            })
        except KiroAuthError as exc:
            if exc.code == "authorization_pending":
                time.sleep(interval)
                continue
            if exc.code == "slow_down":
                interval += 2
                time.sleep(interval)
                continue
            raise
        access, refresh = token.get("accessToken"), token.get("refreshToken")
        if not access or not refresh:
            raise KiroAuthError("AWS OIDC token response omitted access or refresh token")
        creds = Credentials(
            str(access), str(refresh), str(client_id), str(client_secret), region, start_url,
            time.time() + float(token.get("expiresIn") or 3600),
            float(registration.get("clientSecretExpiresAt") or 0),
        )
        return creds, str(uri), str(user_code)
    raise KiroAuthError("Device authorization expired; run login again")


def login(
    start_url: str = BUILDER_ID_START_URL,
    region: str = "us-east-1",
    *,
    label: str | None = None,
    priority: int | None = None,
) -> tuple[str, str]:
    """Run device authorization and save the resulting grant in Hermes' pool."""
    creds, uri, user_code = _device_login(start_url, region)
    persist_credentials(creds, label=label, priority=priority)
    return uri, user_code


def refresh_credential(entry: PooledCredential) -> dict[str, Any]:
    """Rotate one pooled Kiro OAuth grant for Hermes' credential pool."""
    from hermes_cli.auth_constants import AuthError

    creds = _entry_credentials(entry)
    if creds.client_secret_expires_at and time.time() >= creds.client_secret_expires_at:
        raise AuthError(
            "Kiro client registration expired; sign in again.",
            provider=PROVIDER, code="invalid_client", relogin_required=True,
        )
    try:
        token = _post(creds.region, "token", {
            "clientId": creds.client_id,
            "clientSecret": creds.client_secret,
            "grantType": "refresh_token",
            "refreshToken": creds.refresh_token,
        })
    except KiroAuthError as exc:
        if exc.code in _TERMINAL_REFRESH_CODES:
            raise AuthError(
                str(exc), provider=PROVIDER, code=exc.code, relogin_required=True,
            ) from exc
        raise
    access = token.get("accessToken")
    if not access:
        raise KiroAuthError("Kiro token refresh returned no access token")
    return {
        "access_token": str(access),
        "refresh_token": str(token.get("refreshToken") or creds.refresh_token),
        "expires_at_ms": int((time.time() + float(token.get("expiresIn") or 3600)) * 1000),
    }


def get_credentials(force_refresh: bool = False, stale_access_token: str = "") -> Credentials:
    """Select a pooled credential and let Hermes serialize any needed refresh."""
    from agent.credential_pool import load_pool

    pool = load_pool(PROVIDER)
    entries = pool.entries()
    if not entries:
        raise KiroAuthError("Kiro is not logged in. Run `hermes auth add kiro`.")

    entry = next((item for item in entries if stale_access_token and item.access_token == stale_access_token), None)
    if force_refresh:
        if entry is None:
            current = pool.select()
            if current is not None and stale_access_token and current.access_token != stale_access_token:
                return _entry_credentials(current)
            entry = current
        refreshed = pool.try_refresh_matching(credential_id=entry.id if entry else None)
        if refreshed is None:
            raise KiroAuthError("Kiro sign-in could not be refreshed; run `hermes auth add kiro` again.")
        return _entry_credentials(refreshed)

    entry = pool.select()
    if entry is None:
        raise KiroAuthError("No Kiro credential is currently available; check `hermes auth list kiro`.")
    if entry.expires_at_ms is not None and int(entry.expires_at_ms) <= int(time.time() * 1000) + 300_000:
        entry = pool.try_refresh_matching(credential_id=entry.id)
        if entry is None:
            raise KiroAuthError("Kiro sign-in could not be refreshed; run `hermes auth add kiro` again.")
    return _entry_credentials(entry)


def logout() -> bool:
    """Compatibility helper for the old standalone command; native logout is `hermes auth logout kiro`."""
    from agent.credential_pool import load_pool
    from hermes_cli.auth import clear_provider_auth

    had_state = bool(load_pool(PROVIDER).entries())
    clear_provider_auth(PROVIDER)
    return had_state


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
    """Resolve interactive IdC inputs; native `hermes auth` deliberately has no plugin-specific flags."""
    if start_url is None:
        choice = _arrow_login_choice() or input_fn("\nSelect login method:\n\n  1. AWS Builder ID\n  2. IAM Identity Center\n\nChoice [1]: ").strip()
        if choice not in {"", "1", "2"}:
            raise KiroAuthError("Choose 1 for AWS Builder ID or 2 for IAM Identity Center")
        start_url = BUILDER_ID_START_URL if choice in {"", "1"} else input_fn("\nIAM Identity Center start URL:\n\n> ").strip()
    if region is None:
        region = input_fn("\nIAM Identity Center region [us-east-1]:\n\n> ").strip() or "us-east-1"
    return start_url, region


def auth_handler(action: str, args: Any) -> bool:
    """Own Kiro login; status/logout/refresh intentionally use Hermes' generic pool paths."""
    if action != "add":
        return False
    requested_type = str(getattr(args, "auth_type", "") or "").strip().lower().replace("-", "_")
    if requested_type and requested_type not in {"oauth", "oauth_device_code"}:
        raise KiroAuthError("Kiro uses device-code OAuth; omit --type or use `--type oauth`.")
    start_url, region = prompt_login_inputs(None, None)
    login(
        start_url,
        region,
        label=getattr(args, "label", None),
        priority=getattr(args, "priority", None),
    )
    print("Added Kiro OAuth credentials to the Hermes credential pool.")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Kiro IAM Identity Center authentication")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("login")
    sub.add_parser("status")
    args = parser.parse_args()
    if args.command == "login":
        start_url, region = prompt_login_inputs(None, None)
        login(start_url, region)
        print("Kiro login saved in Hermes' credential pool.")
    else:
        creds = get_credentials()
        print(f"logged in; IdC region={creds.region}; runtime region={creds.api_region}; expires_in={int(creds.expires_at-time.time())}s")


if __name__ == "__main__":
    main()
