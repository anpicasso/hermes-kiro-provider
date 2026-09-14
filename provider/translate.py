"""Small OpenAI-chat to Kiro conversationState translation layer."""
from __future__ import annotations

import json
import uuid
from typing import Any

_EMPTY = "Please proceed with the task."
_TOOL = "Tool results provided."


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(p.get("text") or "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _tool_specs(tools: Any) -> list[dict]:
    result = []
    for tool in tools or []:
        fn = tool.get("function", {}) if isinstance(tool, dict) else {}
        if fn.get("name"):
            result.append({"toolSpecification": {"name": fn["name"], "description": fn.get("description") or fn["name"], "inputSchema": {"json": fn.get("parameters") or {"type": "object", "properties": {}}}}})
    return result


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
            history.append({"userInputMessage": {"content": _TOOL, "modelId": model, "origin": "KIRO_CLI", "userInputMessageContext": {"toolResults": [{"toolUseId": message.get("tool_call_id") or "", "status": "error" if message.get("is_error") else "success", "content": [{"text": _text(message.get("content")) or "(no output)"}]}]}}})
    text = _text(current.get("content"))
    if system:
        text = f"{system}\n\n{text}".strip()
    user: dict[str, Any] = {"content": text or (_TOOL if current.get("role") == "tool" else _EMPTY), "modelId": model, "origin": "KIRO_CLI"}
    specs = _tool_specs(tools)
    if current.get("role") == "tool":
        user["userInputMessageContext"] = {"toolResults": [{"toolUseId": current.get("tool_call_id") or "", "status": "error" if current.get("is_error") else "success", "content": [{"text": text or "(no output)"}]}]}
    if specs:
        user.setdefault("userInputMessageContext", {})["tools"] = specs
    state: dict[str, Any] = {"chatTriggerType": "MANUAL", "agentTaskType": "vibe", "conversationId": conversation_id or uuid.uuid4().hex, "currentMessage": {"userInputMessage": user}}
    if history:
        state["history"] = history
    body: dict[str, Any] = {"conversationState": state, "agentMode": "vibe"}
    if effort:
        body["additionalModelRequestFields"] = effort
    return body
