"""One process-wide HTTP pool for Kiro's OIDC, REST and streaming requests."""
from __future__ import annotations

import json
from typing import Any

import urllib3


# ponytail: one shared pool is enough for concurrent Hermes conversations;
# split per-account pools only if the plugin gains multiple accounts/proxies.
POOL = urllib3.PoolManager(num_pools=4, maxsize=20, block=True, retries=False)


class KiroHTTPError(RuntimeError):
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}: {body.decode('utf-8', 'replace')[:500]}")


def request(method: str, url: str, *, body: bytes | None = None, headers: dict[str, str] | None = None, timeout: float = 30, stream: bool = False):
    response = POOL.request(
        method,
        url,
        body=body,
        headers=headers,
        preload_content=not stream,
        timeout=urllib3.Timeout(connect=10, read=timeout),
    )
    if 200 <= response.status < 300:
        return response
    try:
        data = response.read() if stream else response.data
    finally:
        response.release_conn()
    raise KiroHTTPError(response.status, data or b"")


def request_json(method: str, url: str, *, body: bytes | None = None, headers: dict[str, str] | None = None, timeout: float = 30) -> dict[str, Any]:
    response = request(method, url, body=body, headers=headers, timeout=timeout)
    try:
        data = json.loads(response.data or b"{}")
    finally:
        response.release_conn()
    if not isinstance(data, dict):
        raise KiroHTTPError(200, b"Kiro returned malformed JSON")
    return data
