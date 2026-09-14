"""Direct Kiro native transport, shaped like OpenAI's chat client."""
from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
import uuid
from types import SimpleNamespace
from typing import Any, Iterator

from botocore.eventstream import EventStreamBuffer

from credentials import KiroAuthError, get_credentials, save_credentials
from translate import build_request

_FALLBACK = ("claude-sonnet-4.5", "claude-haiku-4.5", "gpt-5.6-terra")
_END = object()


def _management(creds, target: str, payload: dict) -> dict:
    """Kiro's control plane is target-dispatched, not REST-path dispatched."""
    url = f"https://management.{creds.api_region}.kiro.dev/"
    headers = {
        "Accept": "application/json", "Authorization": f"Bearer {creds.access_token}",
        "Content-Type": "application/x-amz-json-1.0", "x-amz-target": target,
        "TokenType": "SSO_OIDC", "amz-sdk-invocation-id": str(uuid.uuid4()),
        "amz-sdk-request": "attempt=1; max=3",
    }
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.loads(response.read() or b"{}")
    return result if isinstance(result, dict) else {}


def list_model_ids() -> list[str]:
    try:
        creds = get_credentials()
        profile_arn = _ensure_profile_arn(creds)
        if not profile_arn:
            return list(_FALLBACK)
        data = _management(creds, "KiroControlPlaneBearerService.ListAvailableModels", {"origin": "KIRO_CLI", "profileArn": profile_arn})
        models = [str(m["modelId"]) for m in data.get("models") or [] if isinstance(m, dict) and isinstance(m.get("modelId"), str)]
        return models or list(_FALLBACK)
    except Exception:
        return list(_FALLBACK)


def _ensure_profile_arn(creds) -> str:
    """Discover the IdC profile once; Builder ID must not make this denied call."""
    if creds.profile_arn:
        return creds.profile_arn
    if getattr(creds, "is_builder_id", False):
        return ""
    data = _management(creds, "KiroControlPlaneBearerService.ListAvailableProfiles", {})
    for profile in data.get("profiles") or []:
        if isinstance(profile, dict) and (arn := str(profile.get("profileArn") or profile.get("arn") or "").strip()):
            creds.profile_arn = arn
            save_credentials(creds)
            return arn
    raise KiroAuthError("Kiro IAM Identity Center returned no profile ARN")


def _chunk(model: str, delta: dict, finish: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(id=f"chatcmpl-{uuid.uuid4().hex}", object="chat.completion.chunk", created=int(time.time()), model=model, choices=[SimpleNamespace(index=0, delta=SimpleNamespace(**delta), finish_reason=finish)])


def _next_or_end(iterator):
    try:
        return next(iterator)
    except StopIteration:
        return _END


class _AwaitableResponse(SimpleNamespace):
    def __await__(self):
        async def done():
            return self
        return done().__await__()


class _HybridStream:
    """One native stream for Hermes' synchronous and auxiliary async callers."""
    def __init__(self, iterator: Iterator[SimpleNamespace]) -> None:
        self._iterator = iterator

    def __iter__(self):
        return self._iterator

    def __await__(self):
        async def done():
            return self
        return done().__await__()

    def __aiter__(self):
        async def iterate():
            while (item := await asyncio.to_thread(_next_or_end, self._iterator)) is not _END:
                yield item
        return iterate()


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
        # ponytail: Builder ID rejects profile discovery; corporate IdC requires the discovered ARN.
        if profile_arn := _ensure_profile_arn(creds):
            body["profileArn"] = profile_arn
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
                while True:
                    try:
                        event = buffer.next()
                    except StopIteration:
                        break
                    if event is None:
                        break
                    try:
                        payload = json.loads(event.payload)
                    except (TypeError, ValueError):
                        payload = {}
                    if isinstance(payload, dict):
                        yield str(event.headers.get(":event-type") or ""), payload

    def _create(self, *, model: str, messages: list[dict], stream: bool = False, tools: Any = None, extra_body: dict | None = None, **_: Any):
        effort = (extra_body or {}).get("reasoning")
        body = build_request(messages, tools, model, effort)
        if stream:
            return self._stream(model, body)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return self._complete(model, body)
        return asyncio.to_thread(self._complete, model, body)

    @staticmethod
    def _add_tool_event(calls: dict[str, dict[str, str]], data: dict) -> None:
        call_id = str(data.get("toolUseId") or uuid.uuid4().hex)
        call = calls.setdefault(call_id, {"name": "", "arguments": ""})
        if data.get("name"):
            call["name"] = str(data["name"])
        if "input" not in data:
            return
        value = data["input"]
        fragment = json.dumps(value, separators=(",", ":")) if isinstance(value, (dict, list)) else str(value or "")
        call["arguments"] += fragment

    @staticmethod
    def _tool_calls(calls: dict[str, dict[str, str]]) -> list[SimpleNamespace]:
        return [SimpleNamespace(index=index, id=call_id, type="function", function=SimpleNamespace(name=value["name"], arguments=value["arguments"] or "{}")) for index, (call_id, value) in enumerate(calls.items())]

    def _stream(self, model: str, body: dict) -> _HybridStream:
        return _HybridStream(self._stream_impl(model, body))

    def _stream_impl(self, model: str, body: dict) -> Iterator[SimpleNamespace]:
        sent = False
        calls: dict[str, dict[str, str]] = {}
        tools_sent = False
        for event, data in self._events(body):
            if event == "assistantResponseEvent" and isinstance(data.get("content"), str) and data["content"]:
                yield _chunk(model, {"role": "assistant" if not sent else None, "content": data["content"]})
                sent = True
            elif event == "toolUseEvent":
                self._add_tool_event(calls, data)
            elif event == "metadataEvent" and data.get("stopReason") == "TOOL_USE":
                if calls:
                    yield _chunk(model, {"role": "assistant" if not sent else None, "tool_calls": self._tool_calls(calls)})
                    tools_sent = sent = True
                yield _chunk(model, {}, "tool_calls")
                return
            elif event in {"error", "exception"} or data.get("error"):
                raise KiroAuthError(f"Kiro stream error: {data.get('message') or data.get('error')}")
        if calls:
            yield _chunk(model, {"role": "assistant" if not sent else None, "tool_calls": self._tool_calls(calls)})
            tools_sent = sent = True
            yield _chunk(model, {}, "tool_calls")
            return
        if not sent and not tools_sent:
            raise KiroAuthError("Kiro closed a successful stream without an event")
        yield _chunk(model, {}, "stop")

    def _complete(self, model: str, body: dict) -> _AwaitableResponse:
        text: list[str] = []
        tool_calls: list[SimpleNamespace] = []
        finish = "stop"
        for chunk in self._stream(model, body):
            delta = chunk.choices[0].delta
            if content := getattr(delta, "content", None):
                text.append(content)
            tool_calls.extend(getattr(delta, "tool_calls", None) or [])
            finish = chunk.choices[0].finish_reason or finish
        message = SimpleNamespace(role="assistant", content="".join(text) or None, tool_calls=tool_calls or None, reasoning=None, reasoning_content=None, reasoning_details=None)
        usage = SimpleNamespace(prompt_tokens=0, completion_tokens=0, total_tokens=0, prompt_tokens_details=SimpleNamespace(cached_tokens=0))
        return _AwaitableResponse(model=model, choices=[SimpleNamespace(index=0, message=message, finish_reason=finish)], usage=usage)
