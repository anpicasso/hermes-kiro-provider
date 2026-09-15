"""Direct Kiro native transport, shaped like OpenAI's chat client."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
import urllib.parse
import uuid
from types import SimpleNamespace
from typing import Any, Iterator

from botocore.eventstream import EventStreamBuffer

from credentials import KiroAuthError, get_credentials, save_credentials
from transport import KiroHTTPError, request, request_json
from translate import build_request

_FALLBACK = ("claude-sonnet-4.5", "claude-haiku-4.5", "gpt-5.6-terra")
_END = object()


def _headers(creds, *, accept: str = "application/json", target: str = "") -> dict[str, str]:
    ua = "aws-sdk-python/1.0 KiroIDE-hermes"
    headers = {
        "Accept": accept,
        "Authorization": f"Bearer {creds.access_token}",
        "TokenType": "SSO_OIDC",
        "User-Agent": ua,
        "x-amz-user-agent": ua,
        "x-amzn-codewhisperer-optout": "true",
        "amz-sdk-invocation-id": str(uuid.uuid4()),
        "amz-sdk-request": "attempt=1; max=3",
    }
    if target:
        headers["Content-Type"] = "application/x-amz-json-1.0"
        headers["x-amz-target"] = target
    return headers


def _management(creds, target: str, payload: dict) -> dict:
    """Kiro's target-dispatched control plane."""
    return request_json(
        "POST",
        f"https://management.{creds.api_region}.kiro.dev/",
        body=json.dumps(payload).encode(),
        headers=_headers(creds, target=target),
    )


def _rest(creds, path: str, query: dict[str, str]) -> dict:
    return request_json(
        "GET",
        f"https://codewhisperer.us-east-1.amazonaws.com/{path}?{urllib.parse.urlencode(query)}",
        headers=_headers(creds),
    )


def list_model_ids() -> list[str]:
    try:
        creds = get_credentials()
        profile_arn = _ensure_profile_arn(creds)
        query = {"origin": "AI_EDITOR", "maxResults": "50"}
        if profile_arn:
            query["profileArn"] = profile_arn
        data = _rest(creds, "ListAvailableModels", query)
        models = [str(m["modelId"]) for m in data.get("models") or [] if isinstance(m, dict) and isinstance(m.get("modelId"), str)]
        return models or list(_FALLBACK)
    except Exception:
        return list(_FALLBACK)


def get_usage_limits() -> dict:
    creds = get_credentials()
    profile_arn = _ensure_profile_arn(creds)
    query = {"isEmailRequired": "true", "origin": "AI_EDITOR", "resourceType": "AGENTIC_REQUEST"}
    if profile_arn:
        query["profileArn"] = profile_arn
    return _rest(creds, "getUsageLimits", query)


def format_usage(data: dict) -> str:
    buckets = data.get("usageBreakdownList") or []
    if not isinstance(buckets, list) or not buckets:
        return "Kiro returned no usage buckets."

    def display(value: object) -> str:
        try:
            return f"{float(str(value)):,.2f}".rstrip("0").rstrip(".")
        except (TypeError, ValueError):
            return str(value)

    lines = ["Kiro usage:"]
    for index, bucket in enumerate(buckets, 1):
        if not isinstance(bucket, dict):
            continue
        current, limit = bucket.get("currentUsage"), bucket.get("usageLimit")
        name = bucket.get("displayName") or bucket.get("usageType") or bucket.get("resourceType") or f"Allowance {index}"
        try:
            current_number, limit_number = float(str(current)), float(str(limit))
            included = min(current_number, limit_number)
            percent = f" ({included / limit_number * 100:.0f}%)" if limit_number > 0 else ""
        except (TypeError, ValueError):
            included, percent = current, ""
        reset = bucket.get("nextDateReset")
        if reset:
            try:
                reset = datetime.fromtimestamp(float(str(reset)), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            except (TypeError, ValueError, OSError):
                pass
        lines.append(f"- {name}: {display(included)}/{display(limit)}{percent}" + (f"; resets {reset}" if reset else ""))

        if any(key in bucket for key in ("currentOverages", "currentOveragesWithPrecision", "overageCap", "overageCharges")):
            overage = bucket.get("currentOveragesWithPrecision", bucket.get("currentOverages", 0))
            cap = bucket.get("overageCapWithPrecision", bucket.get("overageCap", 0))
            try:
                overage_number, cap_number = float(str(overage)), float(str(cap))
                cap_percent = f" ({overage_number / cap_number * 100:.0f}% of cap)" if cap_number > 0 else ""
            except (TypeError, ValueError):
                cap_percent = ""
            plural = str(bucket.get("displayNamePlural") or f"{name}s").lower()
            extra = f"  Extra usage: {display(overage)}/{display(cap)} {plural}{cap_percent}"
            rate, charges, currency = bucket.get("overageRate"), bucket.get("overageCharges"), bucket.get("currency")
            try:
                extra += f"; ${float(str(charges)):.2f} {currency or 'USD'} at ${display(rate)}/{str(name).lower()}"
            except (TypeError, ValueError):
                pass
            lines.append(extra)
    return "\n".join(lines)


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

    def __init__(self, companion_error: str | None = None, **_: Any) -> None:
        self.api_key = "kiro-oauth-local"
        self.base_url = "https://runtime.us-east-1.kiro.dev"
        self.is_closed = False
        self._companion_error = companion_error
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def close(self) -> None:
        self.is_closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _open(self, body: dict, force_refresh: bool = False, stale_access_token: str = ""):
        creds = get_credentials(force_refresh=force_refresh, stale_access_token=stale_access_token)
        # ponytail: Builder ID rejects profile discovery; corporate IdC requires the discovered ARN.
        if profile_arn := _ensure_profile_arn(creds):
            body["profileArn"] = profile_arn
        headers = _headers(
            creds,
            accept="application/vnd.amazon.eventstream",
            target="AmazonCodeWhispererStreamingService.GenerateAssistantResponse",
        )
        headers["x-amzn-kiro-agent-mode"] = "vibe"
        used_access_token = creds.access_token
        try:
            response = request(
                "POST",
                f"https://runtime.{creds.api_region}.kiro.dev/generateAssistantResponse",
                body=json.dumps(body).encode(),
                headers=headers,
                timeout=600,
                stream=True,
            )
        except KiroHTTPError as exc:
            exc.access_token = used_access_token
            raise
        return response, creds.access_token

    def _events(self, body: dict) -> Iterator[tuple[str, dict]]:
        response = None
        stale_access_token = ""
        for attempt in range(2):
            try:
                opened = self._open(body, force_refresh=attempt == 1, stale_access_token=stale_access_token)
                response, stale_access_token = opened if isinstance(opened, tuple) else (opened, "")
                break
            except KiroHTTPError as exc:
                if exc.status not in (401, 403) or attempt:
                    raise KiroAuthError(f"Kiro runtime failed ({exc.status}): {exc.body.decode('utf-8', 'replace')[:500]}") from exc
                stale_access_token = exc.access_token
        if response is None:
            raise KiroAuthError("Kiro runtime could not be reached")
        buffer = EventStreamBuffer()
        try:
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
        finally:
            release = getattr(response, "release_conn", None)
            if release:
                release()

    def _create(self, *, model: str, messages: list[dict], stream: bool = False, tools: Any = None, extra_body: dict | None = None, **_: Any):
        if self._companion_error:
            raise KiroAuthError(self._companion_error)
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
