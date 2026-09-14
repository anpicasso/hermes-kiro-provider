"""Small OpenAI-chat to Kiro conversationState translation layer."""
from __future__ import annotations

import json
import uuid
from typing import Any

_EMPTY = "Please proceed with the task."
_TOOL = "Tool results provided."
_EMPTY_SCHEMA = {"type": "object", "properties": {}}


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(p.get("text") or "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _tool_specs(tools: Any, messages: list[dict]) -> list[dict]:
    """Keep definitions for replayed tool calls; Kiro rejects orphaned results."""
    specs: dict[str, dict] = {}
    for tool in tools or []:
        fn = tool.get("function", {}) if isinstance(tool, dict) else {}
        name = fn.get("name")
        if name:
            specs[name] = {"toolSpecification": {"name": name, "description": fn.get("description") or name, "inputSchema": {"json": fn.get("parameters") or _EMPTY_SCHEMA}}}
    for message in messages:
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            name = fn.get("name")
            if name and name not in specs:
                specs[name] = {"toolSpecification": {"name": name, "description": name, "inputSchema": {"json": _EMPTY_SCHEMA}}}
    return list(specs.values())


def _assistant(message: dict) -> dict:
    uses = []
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        try:
            arguments = json.loads(fn.get("arguments") or "{}")
        except (TypeError, ValueError):
            arguments = {}
        uses.append({"name": fn.get("name") or "", "toolUseId": call.get("id") or uuid.uuid4().hex, "input": arguments})
    item: dict[str, Any] = {"content": _text(message.get("content"))}
    if uses:
        item["toolUses"] = uses
    return {"assistantResponseMessage": item}


def _tool_result(message: dict) -> dict:
    return {"toolUseId": message.get("tool_call_id") or "", "status": "error" if message.get("is_error") else "success", "content": [{"text": _text(message.get("content")) or "(no output)"}]}


def build_request(messages: list[dict], tools: Any, model: str, effort: dict | None, conversation_id: str | None = None) -> dict:
    system = "\n\n".join(_text(m.get("content")) for m in messages if m.get("role") == "system").strip()
    conversation = [m for m in messages if m.get("role") != "system"]
    current = conversation.pop() if conversation and conversation[-1].get("role") in {"user", "tool"} else {"role": "user", "content": ""}
    history: list[dict] = []
    for message in conversation:
        role = message.get("role")
        if role == "assistant":
            history.append(_assistant(message))
        elif role == "user":
            history.append({"userInputMessage": {"content": _text(message.get("content")) or _EMPTY, "modelId": model, "origin": "KIRO_CLI"}})
        elif role == "tool":
            history.append({"userInputMessage": {"content": _TOOL, "modelId": model, "origin": "KIRO_CLI", "userInputMessageContext": {"toolResults": [_tool_result(message)]}}})
    text = _text(current.get("content"))
    if system and current.get("role") == "user":
        text = f"{system}\n\n{text}".strip()
    user: dict[str, Any] = {"content": text or (_TOOL if current.get("role") == "tool" else _EMPTY), "modelId": model, "origin": "KIRO_CLI"}
    context: dict[str, Any] = {}
    if current.get("role") == "tool":
        context["toolResults"] = [_tool_result(current)]
    if specs := _tool_specs(tools, conversation + [current]):
        context["tools"] = specs
    if context:
        user["userInputMessageContext"] = context
    state: dict[str, Any] = {"chatTriggerType": "MANUAL", "agentTaskType": "vibe", "conversationId": conversation_id or uuid.uuid4().hex, "currentMessage": {"userInputMessage": user}}
    if history:
        state["history"] = history
    body: dict[str, Any] = {"conversationState": state, "agentMode": "vibe"}
    if effort:
        body["additionalModelRequestFields"] = effort
    return body
