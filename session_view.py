"""Rebuild a readable conversation from an agent's in-process messages.

`agent.messages` IS the session memory for both demo agents: a list of
Bedrock-converse messages that accumulates across requests and is wiped on
reset or end-of-session. The web console holds its own copy of the thread in
React state, so a browser refresh used to show an empty panel while the agent
still had the whole conversation.

This turns those messages back into the turn shape the console renders, so a
refresh shows what the agent actually remembers rather than a blank slate.

Tool calls and results are recovered where the messages carry them. The
one-line summaries are generic here — the live stream builds nicer ones from
each service's own summarizers, which don't survive in the message log.
"""

from __future__ import annotations

from typing import Any


def _blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content")
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _truncate(value: Any, limit: int = 80) -> str:
    text = value if isinstance(value, str) else str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else f"{text[:limit]}…"


def _result_body(tool_result: dict[str, Any]) -> Any:
    """Unwrap converse toolResult content into something renderable."""
    content = tool_result.get("content")
    if not isinstance(content, list):
        return content
    parts: list[Any] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if "json" in block:
            parts.append(block["json"])
        elif "text" in block:
            parts.append(block["text"])
    if not parts:
        return content
    return parts[0] if len(parts) == 1 else parts


def turns_from_messages(messages: list[Any] | None) -> list[dict[str, Any]]:
    """Group a converse message log into [{user_prompt, trace, final_reply}]."""
    turns: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    pending: dict[str, dict[str, Any]] = {}

    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        blocks = _blocks(message)

        if role == "user":
            texts = [b["text"] for b in blocks if isinstance(b.get("text"), str)]
            if texts:
                # A user text block starts a turn. Tool results also arrive as
                # user messages, but carry no text.
                current = {"user_prompt": "\n".join(texts), "trace": [], "final_reply": ""}
                turns.append(current)
            for block in blocks:
                result = block.get("toolResult") or block.get("tool_result")
                if not isinstance(result, dict):
                    continue
                row = pending.get(str(result.get("toolUseId") or result.get("tool_use_id")))
                if row is None:
                    continue
                body = _result_body(result)
                row["result"] = body
                row["is_error"] = result.get("status") == "error"
                row["result_summary"] = _truncate(body)

        elif role == "assistant" and current is not None:
            texts = [b["text"] for b in blocks if isinstance(b.get("text"), str)]
            if texts:
                # The last assistant text of a turn is the reply the customer saw.
                current["final_reply"] = "\n".join(texts)
            for block in blocks:
                use = block.get("toolUse") or block.get("tool_use")
                if not isinstance(use, dict):
                    continue
                tool_use_id = str(use.get("toolUseId") or use.get("id") or len(pending))
                args = use.get("input") or {}
                row = {
                    "tool_use_id": tool_use_id,
                    "name": str(use.get("name", "tool")),
                    "args": args,
                    "args_summary": _truncate(
                        ", ".join(f"{k}={v}" for k, v in args.items())
                        if isinstance(args, dict)
                        else args
                    ),
                }
                current["trace"].append(row)
                pending[tool_use_id] = row

    return turns
