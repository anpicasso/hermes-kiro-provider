from __future__ import annotations

import json
import sys
import asyncio
import struct
import threading
import time
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "provider"))

import client
import credentials
from credentials import BUILDER_ID_START_URL, KiroAuthError, _next_login_choice, prompt_login_inputs, runtime_region, validate_start_url
from translate import build_request


def test_start_url_is_strict_and_region_maps():
    assert validate_start_url(BUILDER_ID_START_URL) == BUILDER_ID_START_URL
    assert validate_start_url("https://d-abc.awsapps.com/start/") == "https://d-abc.awsapps.com/start"
    assert runtime_region("eu-west-1") == "eu-central-1"
    with pytest.raises(KiroAuthError):
        validate_start_url("http://evil.example/start")
    with pytest.raises(KiroAuthError):
        validate_start_url("https://notawsapps.com/start")
    with pytest.raises(KiroAuthError):
        runtime_region("attacker.example")


def test_interactive_login_defaults_to_builder_id_or_accepts_custom_idc():
    defaults = iter(["", ""])
    custom = iter(["2", "https://d-abc.awsapps.com/start", "eu-west-1"])
    assert prompt_login_inputs(None, None, lambda _: next(defaults)) == (BUILDER_ID_START_URL, "us-east-1")
    assert prompt_login_inputs(None, None, lambda _: next(custom)) == ("https://d-abc.awsapps.com/start", "eu-west-1")


def test_login_selector_moves_with_arrow_keys_and_fallback_is_spaced():
    assert _next_login_choice(0, "down") == 1
    assert _next_login_choice(1, "up") == 0
    prompts = []
    values = iter(["", ""])
    prompt_login_inputs(None, None, lambda text: prompts.append(text) or next(values))
    assert "\n  1. AWS Builder ID\n  2. IAM Identity Center\n\n" in prompts[0]


def test_request_hoists_system_and_never_sends_empty_user_content():
    request = build_request([
        {"role": "system", "content": "Be concise."},
        {"role": "user", "content": ""},
    ], [], "claude-sonnet-4.5", None, "c")
    message = request["conversationState"]["currentMessage"]["userInputMessage"]
    assert message["content"] == "Be concise."
    assert "system" not in json.dumps(request)


def test_nonstream_facade_returns_openai_shape(monkeypatch):
    instance = client.KiroClient()
    monkeypatch.setattr(instance, "_events", lambda _: iter([
        ("assistantResponseEvent", {"content": "hello"}),
        ("assistantResponseEvent", {"content": " world"}),
    ]))
    response = instance.chat.completions.create(model="claude-sonnet-4.5", messages=[{"role": "user", "content": "hi"}])
    assert response.choices[0].message.content == "hello world"
    assert response.usage.total_tokens == 0


def test_empty_success_stream_is_an_error(monkeypatch):
    instance = client.KiroClient()
    monkeypatch.setattr(instance, "_events", lambda _: iter(()))
    with pytest.raises(KiroAuthError, match="without an event"):
        list(instance.chat.completions.create(model="claude-sonnet-4.5", messages=[{"role": "user", "content": "hi"}], stream=True))


def test_provider_client_explains_when_login_companion_is_missing():
    instance = client.KiroClient(companion_error="Install the Kiro login-command companion.")
    with pytest.raises(KiroAuthError, match="login-command companion"):
        instance.chat.completions.create(model="claude-sonnet-4.5", messages=[{"role": "user", "content": "hi"}])


def test_each_component_explains_its_missing_companion(monkeypatch, tmp_path):
    import commands
    import provider

    monkeypatch.setattr(commands, "_provider_dir", lambda: tmp_path / "kiro-provider")
    assert "provider companion" in commands.handle_kiro_slash("status")

    monkeypatch.setattr(provider, "_commands_companion_error", lambda: "Install the Kiro login-command companion.")
    with pytest.raises(KiroAuthError, match="login-command companion"):
        provider.profile.fetch_models()
    instance = provider.profile.create_client()
    with pytest.raises(KiroAuthError, match="login-command companion"):
        instance.chat.completions.create(model="claude-sonnet-4.5", messages=[{"role": "user", "content": "hi"}])


def test_event_decoder_handles_a_frame_split_mid_prelude(monkeypatch):
    class Response:
        def __init__(self):
            payload = json.dumps({"content": "OK"}).encode()
            event_type = b"assistantResponseEvent"
            headers = b"\x0b:event-type\x07" + struct.pack(">H", len(event_type)) + event_type
            prelude = struct.pack(">II", 16 + len(headers) + len(payload), len(headers))
            prelude += struct.pack(">I", zlib.crc32(prelude) & 0xFFFFFFFF)
            frame = prelude + headers + payload
            frame += struct.pack(">I", zlib.crc32(frame) & 0xFFFFFFFF)
            self.reads = iter([frame[:5], frame[5:], b""])

        def read(self, _):
            return next(self.reads)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

    instance = client.KiroClient()
    monkeypatch.setattr(instance, "_open", lambda *_, **__: Response())
    assert list(instance._events({})) == [("assistantResponseEvent", {"content": "OK"})]


def test_builder_id_request_omits_profile_arn(monkeypatch):
    instance = client.KiroClient()
    captured = {}
    creds = SimpleNamespace(api_region="us-east-1", access_token="token", profile_arn="", is_builder_id=True)
    monkeypatch.setattr(client, "get_credentials", lambda **_: creds)
    monkeypatch.setattr(client, "request", lambda method, url, **kwargs: captured.update(method=method, url=url, **kwargs) or SimpleNamespace())
    instance._open({"conversationState": {}})
    assert "profileArn" not in json.loads(captured["body"])


def test_idc_discovers_profile_arn_without_prompting(monkeypatch):
    instance = client.KiroClient()
    captured = {}
    calls = []
    creds = SimpleNamespace(api_region="us-east-1", access_token="token", profile_arn="", is_builder_id=False)
    monkeypatch.setattr(client, "get_credentials", lambda **_: creds)
    monkeypatch.setattr(client, "save_credentials", lambda _: None)
    monkeypatch.setattr(client, "_management", lambda *_: calls.append(True) or {"profiles": [{"arn": "arn:aws:codewhisperer:us-east-1:1:profile/team"}]})
    monkeypatch.setattr(client, "request", lambda method, url, **kwargs: captured.update(method=method, url=url, **kwargs) or SimpleNamespace())
    instance._open({"conversationState": {}})
    assert calls and creds.profile_arn.endswith("profile/team")
    assert json.loads(captured["body"])["profileArn"] == creds.profile_arn


def test_builder_id_uses_live_bare_model_catalog_and_usage(monkeypatch):
    creds = SimpleNamespace(api_region="us-east-1", access_token="token", profile_arn="", is_builder_id=True)
    calls = []
    monkeypatch.setattr(client, "get_credentials", lambda **_: creds)
    monkeypatch.setattr(client, "_rest", lambda _creds, path, query: calls.append((path, query)) or ({"models": [{"modelId": "live-model"}]} if path == "ListAvailableModels" else {"usageBreakdownList": [{"usageType": "Agent", "currentUsage": 3, "usageLimit": 10}]}))
    assert client.list_model_ids() == ["live-model"]
    assert client.get_usage_limits()["usageBreakdownList"][0]["currentUsage"] == 3
    assert calls == [
        ("ListAvailableModels", {"origin": "AI_EDITOR", "maxResults": "50"}),
        ("getUsageLimits", {"isEmailRequired": "true", "origin": "AI_EDITOR", "resourceType": "AGENTIC_REQUEST"}),
    ]
    assert "3/10 (30%)" in client.format_usage({"usageBreakdownList": [{"usageType": "Agent", "currentUsage": 3, "usageLimit": 10}]})


def test_refresh_is_single_flight_for_concurrent_expired_requests(monkeypatch):
    creds = credentials.Credentials("old", "refresh", "id", "secret", "us-east-1", BUILDER_ID_START_URL, 0)
    calls = []
    monkeypatch.setattr(credentials, "_CACHED", None)
    monkeypatch.setattr(credentials, "_read", lambda: creds)
    monkeypatch.setattr(credentials, "save_credentials", lambda value: None)
    monkeypatch.setattr(credentials, "_post", lambda *_: calls.append(True) or {"accessToken": "new", "expiresIn": 3600})
    results = []
    threads = [threading.Thread(target=lambda: results.append(credentials.get_credentials().access_token)) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert calls == [True]
    assert results == ["new"] * 8


def test_concurrent_401_refreshes_once(monkeypatch):
    creds = credentials.Credentials("old", "refresh", "id", "secret", "us-east-1", BUILDER_ID_START_URL, time.time() + 3600)
    barrier = threading.Barrier(2)
    refreshes, retried, errors = [], [], []

    class Response:
        def read(self, _):
            return b""
        def release_conn(self):
            return None

    def fake_request(*_, headers, **__):
        token = headers["Authorization"]
        if token == "Bearer old":
            barrier.wait(timeout=2)
            raise client.KiroHTTPError(401, b"expired")
        retried.append(token)
        return Response()

    monkeypatch.setattr(credentials, "_CACHED", None)
    monkeypatch.setattr(credentials, "_read", lambda: creds)
    monkeypatch.setattr(credentials, "save_credentials", lambda value: None)
    monkeypatch.setattr(credentials, "_post", lambda *_: refreshes.append(True) or {"accessToken": "new", "expiresIn": 3600})
    monkeypatch.setattr(client, "get_credentials", credentials.get_credentials)
    monkeypatch.setattr(client, "request", fake_request)

    def run():
        try:
            list(client.KiroClient()._events({"conversationState": {}}))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert refreshes == [True]
    assert retried == ["Bearer new", "Bearer new"]


def test_logout_removes_only_hermes_kiro_state(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(credentials, "_CACHED", None)
    (tmp_path / ".env").write_text("OTHER=value\nKIRO_AUTH=kiro-oauth-local\n")
    credentials._write(credentials.Credentials("a", "r", "id", "secret", "us-east-1", BUILDER_ID_START_URL, time.time() + 60))
    assert credentials.logout() is True
    assert not credentials.credential_path().exists()
    assert (tmp_path / ".env").read_text() == "OTHER=value\n"


def test_tool_calls_round_trip_and_nonstream_is_awaitable(monkeypatch):
    instance = client.KiroClient()
    monkeypatch.setattr(instance, "_events", lambda _: iter([
        ("toolUseEvent", {"name": "read_file", "toolUseId": "call_1", "input": {"path": "x"}}),
        ("metadataEvent", {"stopReason": "TOOL_USE"}),
    ]))
    response = instance.chat.completions.create(model="claude-sonnet-4.5", messages=[{"role": "user", "content": "hi"}])
    call = response.choices[0].message.tool_calls[0]
    assert response.choices[0].finish_reason == "tool_calls"
    assert (call.id, call.function.name, call.function.arguments) == ("call_1", "read_file", '{"path":"x"}')

    async def get_response():
        return await instance.chat.completions.create(model="claude-sonnet-4.5", messages=[{"role": "user", "content": "hi"}])

    assert asyncio.run(get_response()).choices[0].message.tool_calls[0].function.name == "read_file"

    async def get_stream():
        chunks = []
        async for chunk in await instance.chat.completions.create(model="claude-sonnet-4.5", messages=[{"role": "user", "content": "hi"}], stream=True):
            chunks.append(chunk)
        return chunks

    assert asyncio.run(get_stream())[-1].choices[0].finish_reason == "tool_calls"


def test_async_nonstream_does_not_block_event_loop(monkeypatch):
    instance = client.KiroClient()

    def slow_events(_):
        time.sleep(0.2)
        yield "assistantResponseEvent", {"content": "OK"}

    monkeypatch.setattr(instance, "_events", slow_events)

    async def run():
        started = time.monotonic()
        task = asyncio.create_task(instance.chat.completions.create(model="claude-sonnet-4.5", messages=[{"role": "user", "content": "hi"}]))
        await asyncio.sleep(0.01)
        assert time.monotonic() - started < 0.1
        return await task

    assert asyncio.run(run()).choices[0].message.content == "OK"


def test_tool_history_keeps_specs_and_does_not_mix_system_with_result():
    request = build_request([
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "Read x"},
        {"role": "assistant", "tool_calls": [{"id": "call_1", "function": {"name": "read_file", "arguments": '{"path":"x"}'}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "/tmp/x"},
    ], [], "claude-sonnet-4.5", None, "c")
    current = request["conversationState"]["currentMessage"]["userInputMessage"]
    tools = current["userInputMessageContext"]["tools"]
    result = current["userInputMessageContext"]["toolResults"][0]
    assert tools[0]["toolSpecification"]["name"] == "read_file"
    assert result["content"] == [{"text": "/tmp/x"}]
    assert "SYSTEM" in request["conversationState"]["history"][0]["userInputMessage"]["content"]


def test_parallel_tool_results_share_one_user_turn():
    request = build_request([
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "Read x and y"},
        {"role": "assistant", "tool_calls": [
            {"id": "call_x", "function": {"name": "read_file", "arguments": '{"path":"x"}'}},
            {"id": "call_y", "function": {"name": "read_file", "arguments": '{"path":"y"}'}},
        ]},
        {"role": "tool", "tool_call_id": "call_x", "content": "x"},
        {"role": "tool", "tool_call_id": "call_y", "content": "y"},
    ], [], "claude-sonnet-4.5", None, "c")
    state = request["conversationState"]
    assert len(state["history"]) == 2
    assert [item["text"] for item in state["currentMessage"]["userInputMessage"]["userInputMessageContext"]["toolResults"][0]["content"]] == ["x"]
    assert len(state["currentMessage"]["userInputMessage"]["userInputMessageContext"]["toolResults"]) == 2
