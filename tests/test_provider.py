from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "provider"))

import client
from credentials import BUILDER_ID_START_URL, KiroAuthError, runtime_region, validate_start_url
from translate import build_request


def test_start_url_is_strict_and_region_maps():
    assert validate_start_url(BUILDER_ID_START_URL) == BUILDER_ID_START_URL
    assert validate_start_url("https://d-abc.awsapps.com/start/") == "https://d-abc.awsapps.com/start"
    assert runtime_region("eu-west-1") == "eu-central-1"
    with pytest.raises(KiroAuthError):
        validate_start_url("http://evil.example/start")
    with pytest.raises(KiroAuthError):
        runtime_region("attacker.example")


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
