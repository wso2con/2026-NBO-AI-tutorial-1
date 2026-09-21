"""The one model call a turn is allowed after its token budget guard fires.

Shared module, same shape as `planner.py`: a small, non-streaming completion
with no tools attached. The loop has already been paused by
`run_control.TokenBudgetHook`; this call reads the paused session memory and
turns it into something a customer can answer.

It is a side channel. Its output is shown to the customer and then thrown
away: it is never written back into the agent's conversation, because the
paused loop must resume from exactly the context it stopped on.

No tools is the point. A model that still holds tools, asked to "wrap up",
frequently makes one more call instead — which is exactly the spend the guard
just refused. Here the only thing it can produce is prose, and the harness
caps it at a couple of hundred output tokens, so the cost of the wrap-up is
known before it is made.
"""

from __future__ import annotations

import json
import os
from typing import Any

from openai import AsyncOpenAI

from loop_state import TokenSplit, openai_usage

_SYSTEM_PROMPT = """You are a customer support agent whose current turn just hit \
its token budget. The work is paused, not finished, and no further tools will run.

Write a short reply to the customer that does three things and nothing else. \
Write it in the same language the customer's request is written in:
1. States plainly what you established so far, using only the observations listed \
   below. Never invent a finding, a number or an outcome that is not in them.
2. Says what is still unfinished.
3. Asks whether they want you to continue the remaining work. Make clear that \
   continuing means spending more on the task, and that the alternative is to \
   stop here.

Do not apologise at length, do not mention tokens, budgets, tools or internal \
machinery, and do not promise anything you have not already verified. Four \
sentences at most."""


def _clip(text: str, limit: int = 400) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _transcript(messages: list[dict[str, Any]], limit: int = 24) -> str:
    """Flatten the paused session memory into the evidence the reply may use.

    Session memory, not a separate log: the wrap-up describes the same context
    the loop will resume from, so what the customer is told and what the agent
    continues with cannot drift apart.
    """
    lines: list[str] = []
    for message in messages[-limit:]:
        role = message.get("role", "?")
        content = message.get("content")
        if isinstance(content, str):
            lines.append(f"{role}: {_clip(content)}")
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("text"):
                lines.append(f"{role}: {_clip(str(block['text']))}")
            elif tool_use := (block.get("toolUse") or block.get("tool_use")):
                args = json.dumps(tool_use.get("input") or {}, default=str)
                lines.append(f"called {tool_use.get('name', '?')}({_clip(args, 160)})")
            elif tool_result := (block.get("toolResult") or block.get("tool_result")):
                body = tool_result.get("content")
                lines.append(f"observed: {_clip(json.dumps(body, default=str))}")
    return "\n".join(lines) or "(nothing established yet)"


async def wrapup_reply(
    *,
    model: str,
    goal: str,
    session_messages: list[dict[str, Any]],
    next_action: str | None = None,
) -> tuple[str, TokenSplit]:
    """Produce the paused turn's customer-facing reply.

    Returns `(reply, usage)`. Raises on API failure; callers fall back to a
    harness-written sentence so a paused turn always ends with something the
    customer can answer."""
    unfinished = (
        f"The next step would have been: {next_action}."
        if next_action
        else "The remaining checks for this request have not been made."
    )
    client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"What the customer asked for:\n{goal}\n\n"
                    f"The session so far:\n{_transcript(session_messages)}\n\n"
                    f"What is unfinished:\n{unfinished}"
                ),
            },
        ],
        temperature=0.2,
        max_completion_tokens=300,
    )
    return (response.choices[0].message.content or "").strip(), openai_usage(response)
