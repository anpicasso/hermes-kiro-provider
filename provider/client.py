"""Direct Kiro native transport, shaped like OpenAI's chat client."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from types import SimpleNamespace
from typing import Any, Iterator

from botocore.eventstream import EventStreamBuffer

from credentials import KiroAuthError, get_credentials
from translate import build_request

_FALLBACK = ("claude-sonnet-4.5", "claude-haiku-4.5", "gpt-5.6-terra")


def _management(creds, path: str, *, params: dict | None = None, method: str = "GET") -> dict:
    url = f"https://management.{creds.api_region}.kiro.dev/{path}"
    data = None
    if method == "GET" and params:
        url += "?" + urllib.parse.urlencode(params)
    elif method == "POST":
        data = json.dumps(params or {}).encode()
    headers = {"Accept": "application/json", "Authorization": f"Bearer {creds.access_token}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.loads(response.read() or b"{}")
    return result if isinstance(result, dict) else {}


def _profile_arn(creds) -> str:
    if creds.profile_arn:
        return creds.profile_arn
    profiles = _management(creds, "List-Available-Profiles", params={}, method="POST").get("profiles") or []
    arn = next((p.get("arn") for p in profiles if isinstance(p, dict) and p.get("arn")), "")
    if not arn:
        raise KiroAuthError("Kiro returned no available profile")
    creds.profile_arn = arn
    return arn


def list_model_ids() -> list[str]:
    try:
        creds = get_credentials()
        data = _management(creds, "List-Available-Models", params={"profileArn": _profile_arn(creds), "origin": "KIRO_CLI"})
        models = [str(m["modelId"]) for m in data.get("models") or [] if isinstance(m, dict) and isinstance(m.get("modelId"), str)]
        return models or list(_FALLBACK)
    except Exception:
        return list(_FALLBACK)


def _chunk(model: str, delta: dict, finish: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(id=f"chatcmpl-{uuid.uuid4().hex}", object="chat.completion.chunk", created=int(time.time()), model=model, choices=[SimpleNamespace(index=0, delta=SimpleNamespace(**delta), finish_reason=finish)])


class KiroClient:
    HERMES_SKIP_TRANSPORT_WRAP = True
    HERMES_SKIP_ASYNC_WRAP = True

    def __init__(self, **_: Any) -> None:
        self.api_key = "kiro-oauth-local"
        self.base_url = "https://runtime.us-east-1.kiro.dev"
        self.is_closed = False
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def close(self) -> None:
        self.is_closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _open(self, body: dict, force_refresh: bool = False):
        creds = get_credentials(force_refresh=force_refresh)
        body["profileArn"] = _profile_arn(creds)
        ua = "aws-sdk-python/1.0 KiroIDE-hermes"
        request = urllib.request.Request(f"https://runtime.{creds.api_region}.kiro.dev/generateAssistantResponse", data=json.dumps(body).encode(), method="POST", headers={"Authorization": f"Bearer {creds.access_token}", "Content-Type": "application/x-amz-json-1.0", "Accept": "application/vnd.amazon.eventstream", "x-amz-target": "AmazonCodeWhispererStreamingService.GenerateAssistantResponse", "x-amzn-codewhisperer-optout": "true", "x-amzn-kiro-agent-mode": "vibe", "amz-sdk-invocation-id": str(uuid.uuid4()), "amz-sdk-request": "attempt=1; max=2", "User-Agent": ua, "x-amz-user-agent": ua})
        return urllib.request.urlopen(request, timeout=600)

    def _events(self, body: dict) -> Iterator[tuple[str, dict]]:
        response = None
        for attempt in range(2):
            try:
                response = self._open(body, force_refresh=attempt == 1)
                break
            except urllib.error.HTTPError as exc:
                if exc.code not in (401, 403) or attempt:
                    raise KiroAuthError(f"Kiro runtime failed ({exc.code}): {exc.read().decode('utf-8', 'replace')[:500]}") from exc
        if response is None:
            raise KiroAuthError("Kiro runtime could not be reached")
        buffer = EventStreamBuffer()
        with response:
            while raw := response.read(8192):
                buffer.add_data(raw)
                while event := buffer.next():
                    try:
                        payload = json.loads(event.payload)
                    except (TypeError, ValueError):
                        payload = {}
                    if isinstance(payload, dict):
                        yield str(event.headers.get(":event-type") or ""), payload

    def _create(self, *, model: str, messages: list[dict], stream: bool = False, tools: Any = None, extra_body: dict | None = None, **_: Any):
        effort = (extra_body or {}).get("reasoning")
        body = build_request(messages, tools, model, effort)
        return self._stream(model, body) if stream else self._complete(model, body)

    def _stream(self, model: str, body: dict) -> Iterator[SimpleNamespace]:
        sent = False
        for event, data in self._events(body):
            if event == "assistantResponseEvent" and isinstance(data.get("content"), str) and data["content"]:
                yield _chunk(model, {"role": "assistant" if not sent else None, "content": data["content"]})
                sent = True
            elif event == "metadataEvent" and data.get("stopReason") == "TOOL_USE":
                yield _chunk(model, {}, "tool_calls")
                return
            elif event in {"error", "exception"} or data.get("error"):
                raise KiroAuthError(f"Kiro stream error: {data.get('message') or data.get('error')}")
        if not sent:
            raise KiroAuthError("Kiro closed a successful stream without an event")
        yield _chunk(model, {}, "stop")

    def _complete(self, model: str, body: dict) -> SimpleNamespace:
        text = "".join(getattr(chunk.choices[0].delta, "content", None) or "" for chunk in self._stream(model, body))
        message = SimpleNamespace(role="assistant", content=text or None, tool_calls=None, reasoning=None, reasoning_content=None, reasoning_details=None)
        usage = SimpleNamespace(prompt_tokens=0, completion_tokens=0, total_tokens=0, prompt_tokens_details=SimpleNamespace(cached_tokens=0))
        return SimpleNamespace(model=model, choices=[SimpleNamespace(index=0, message=message, finish_reason="stop")], usage=usage)
