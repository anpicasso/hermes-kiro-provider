"""Kiro IAM Identity Center auth backed by Hermes' credential pool."""
from __future__ import annotations

import argparse
import datetime
import json
import sqlite3
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
SOCIAL_AUTH_URL = "https://prod.us-east-1.auth.desktop.kiro.dev"
SOCIAL_START_URL = "social"
KIRO_CLI_DB = Path.home() / ".local/share/kiro-cli/data.sqlite3"
_PROFILE_ARNS: dict[str, str] = {}


class KiroAuthError(RuntimeError):
    def __init__(self, message: str, *, code: str = "", status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        # Hermes' main/auxiliary error matchers read ``status_code`` first; keep
        # the HTTP status visible even though this facade is not an SDK error.
        self.status_code = status


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
    if extra.get("kiro_login") == "social":
        return Credentials(
            access_token=entry.access_token,
            refresh_token=entry.refresh_token or "",
            client_id="", client_secret="",
            region=str(extra.get("region") or "us-east-1"),
            start_url=SOCIAL_START_URL,
            expires_at=float(entry.expires_at_ms or 0) / 1000.0,
            profile_arn=str(extra.get("profile_arn") or _PROFILE_ARNS.get(entry.id) or ""),
            credential_id=entry.id,
        )
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

    extra = {
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "region": creds.region,
        "start_url": creds.start_url,
        "client_secret_expires_at": creds.client_secret_expires_at,
    }
    if creds.start_url == SOCIAL_START_URL:
        extra = {"kiro_login": "social", "region": creds.region, "profile_arn": creds.profile_arn}
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
        extra=extra,
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


def _refresh_social(creds: Credentials) -> dict[str, Any]:
    from hermes_cli.auth_constants import AuthError

    try:
        token = request_json(
            "POST", f"{SOCIAL_AUTH_URL}/refreshToken",
            body=json.dumps({"refreshToken": creds.refresh_token}).encode(),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
    except KiroHTTPError as exc:
        code = _error_code(exc.body)
        if exc.status in (400, 401, 403):
            raise AuthError(
                f"Kiro social refresh rejected ({exc.status}): sign in with kiro-cli and re-import.",
                provider=PROVIDER, code=code or "invalid_grant", relogin_required=True,
            ) from exc
        raise KiroAuthError(f"Kiro social refresh failed ({exc.status})", status=exc.status) from exc
    access = token.get("accessToken")
    if not access:
        raise KiroAuthError("Kiro social refresh returned no access token")
    return {
        "access_token": str(access),
        "refresh_token": str(token.get("refreshToken") or creds.refresh_token),
        "expires_at_ms": int((time.time() + float(token.get("expiresIn") or 3600)) * 1000),
    }


def import_kiro_cli(*, label: str | None = None, priority: int | None = None) -> Credentials:
    """Import the Google/GitHub (social) login that kiro-cli already holds."""
    if not KIRO_CLI_DB.exists():
        raise KiroAuthError(f"kiro-cli database not found at {KIRO_CLI_DB}; run `kiro-cli login` first.")
    con = sqlite3.connect(f"file:{KIRO_CLI_DB}?mode=ro", uri=True)
    try:
        row = con.execute("select value from auth_kv where key = 'kirocli:social:token'").fetchone()
    finally:
        con.close()
    if not row:
        raise KiroAuthError("kiro-cli has no social (Google/GitHub) login; run `kiro-cli login` first.")
    data = json.loads(row[0])
    access, refresh = data.get("access_token"), data.get("refresh_token")
    if not access or not refresh:
        raise KiroAuthError("kiro-cli social token is missing access or refresh token")
    try:
        expires = datetime.datetime.fromisoformat(str(data["expires_at"]).replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError):
        expires = 0.0
    creds = Credentials(
        str(access), str(refresh), "", "", "us-east-1", SOCIAL_START_URL, expires,
        profile_arn=str(data.get("profile_arn") or ""),
    )
    persist_credentials(creds, label=label or f"kiro-{str(data.get('provider') or 'social').lower()}",
                        priority=priority, source="manual:kiro_cli_import")
    return creds


def refresh_credential(entry: PooledCredential) -> dict[str, Any]:
    """Rotate one pooled Kiro OAuth grant for Hermes' credential pool."""
    from hermes_cli.auth_constants import AuthError

    creds = _entry_credentials(entry)
    if creds.start_url == SOCIAL_START_URL:
        return _refresh_social(creds)
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

    pool = load_pool(PROVIDER)
    had_state = bool(pool.entries())
    while pool.entries():
        pool.remove_index(1)
    return had_state


def _next_login_choice(selected: int, key: str) -> int:
    if key == "up":
        return max(0, selected - 1)
    if key == "down":
        return min(2, selected + 1)
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
        options = ("AWS Builder ID", "IAM Identity Center", "Import Google/GitHub login from kiro-cli")
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
            elif key in {"1", "2", "3"}:
                selected = int(key) - 1
                draw()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)
        sys.stdout.write("\x1b[?25h\n")
        sys.stdout.flush()


def prompt_login_inputs(start_url: str | None, region: str | None, input_fn=input) -> tuple[str, str]:
    """Resolve interactive IdC inputs; native `hermes auth` deliberately has no plugin-specific flags."""
    if start_url is None:
        choice = _arrow_login_choice() or input_fn("\nSelect login method:\n\n  1. AWS Builder ID\n  2. IAM Identity Center\n  3. Import Google/GitHub login from kiro-cli\n\nChoice [1]: ").strip()
        if choice == "3":
            return SOCIAL_START_URL, "us-east-1"
        if choice not in {"", "1", "2"}:
            raise KiroAuthError("Choose 1, 2 or 3")
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
    if start_url == SOCIAL_START_URL:
        import_kiro_cli(label=getattr(args, "label", None), priority=getattr(args, "priority", None))
        print("Imported kiro-cli Google/GitHub login into the Hermes credential pool.")
        return True
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
        if start_url == SOCIAL_START_URL:
            import_kiro_cli()
        else:
            login(start_url, region)
        print("Kiro login saved in Hermes' credential pool.")
    else:
        creds = get_credentials()
        print(f"logged in; IdC region={creds.region}; runtime region={creds.api_region}; expires_in={int(creds.expires_at-time.time())}s")


if __name__ == "__main__":
    main()
