from __future__ import annotations

import json
import sys
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "provider"))

import client
from credentials import BUILDER_ID_START_URL, KiroAuthError, prompt_login_inputs, runtime_region, validate_start_url
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


def test_builder_id_request_omits_profile_arn(monkeypatch):
    instance = client.KiroClient()
    captured = {}
    creds = SimpleNamespace(api_region="us-east-1", access_token="token", profile_arn="")
    monkeypatch.setattr(client, "get_credentials", lambda **_: creds)
    monkeypatch.setattr(client.urllib.request, "urlopen", lambda request, timeout: captured.setdefault("request", request))
    instance._open({"conversationState": {}})
    assert "profileArn" not in json.loads(captured["request"].data)


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
        return await response

    assert asyncio.run(get_response()) is response

    async def get_stream():
        chunks = []
        async for chunk in await instance.chat.completions.create(model="claude-sonnet-4.5", messages=[{"role": "user", "content": "hi"}], stream=True):
            chunks.append(chunk)
        return chunks

    assert asyncio.run(get_stream())[-1].choices[0].finish_reason == "tool_calls"


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
