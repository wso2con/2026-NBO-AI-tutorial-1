"""FastAPI entrypoint for cs_agent_engineered — the hardened, production-shaped agent.

Run:  uvicorn main:app --reload --port 8002

Endpoints:
    POST /api/run    SSE stream of agent events for one prompt.
    POST /api/reset  Wipe this agent's mock data + episodic memory.
    GET  /health     Liveness probe.

The browser fans out the same prompt to first-cut's port (8001) and this port
(8002) in parallel, then renders the two SSE streams side by side. No
dispatcher service is needed — the merge happens in the frontend.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

# The shared mocks/ package lives at the lab root, next to cs_agent_engineered/.
# Put it on sys.path before anything imports it (including MCP subprocesses,
# which inherit our environment).
_LAB_ROOT = Path(__file__).parent.parent
if str(_LAB_ROOT) not in sys.path:
    sys.path.insert(0, str(_LAB_ROOT))

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

# Load .env BEFORE importing the agent — OPENAI_API_KEY must be visible
# to the OpenAI client and to MCP subprocesses (which inherit env).
# Single `.env` at the repo root is the canonical place; a per-agent
# `cs_agent_engineered/.env` is supported as an optional override.
load_dotenv(_LAB_ROOT / ".env")
load_dotenv(Path(__file__).parent / ".env", override=False)

from agent.core import ROOT, build_agent, frame_prompt, prepend_memory
from agent.hooks import is_affirmative
from agent.profile import apply_overrides, load_profile
from agent.skill_contracts import load_skill_contract
from budget_wrapup import wrapup_reply
from planner import format_tool_specs, plan_for_prompt, skills_catalogue_from_dir
from policy_evaluator import (
    ASPECTS as EVALUATION_ASPECTS,
    aggregate_verdict,
    evaluate_turn,
)
from mocks.client import arm_fault, disarm_fault
from run_control import BUDGET_STOP_MARKER
from loop_state import (
    RunStore,
    add_auxiliary,
    apply_task_contract,
    context_snapshot,
    event as loop_event,
    grant_budget,
    loop_tokens_used,
    record_iteration,
    record_human_decision,
    record_observation,
    token_ceiling,
    token_warning_threshold,
    usage_payload,
)
from strands import Agent
from strands.models.openai import OpenAIModel

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("cs_agent_engineered")

PROFILE = load_profile()
EPISODIC_DIR = ROOT / "memory" / "episodic"

# Agents are expensive to build (MCP subprocess spawn) and the cached
# instance also IS the conversation memory — `agent.messages` accumulates
# across requests for the same customer, keeping conversation history alive
# for the server's lifetime. Per-customer cache key gives each user their
# own isolated history. In production each customer would map to a
# session/JWT, not a header.
_AGENTS: dict[str, Agent] = {}
_RUNS = RunStore()

# Confirmations awaiting a human answer, keyed by customer_id. One entry is
# written when `HumanConfirmationHook` interrupts the loop before a write tool
# (see agent/hooks.py) and popped by the next /api/run for that customer, which
# resumes the same Agent with the answer instead of starting a new turn.
#
# Process-local and deliberately not persisted: a restart drops the pending
# approval, and a dropped approval means the write does not happen. In
# production this would be a row in the session store with a TTL, but the
# fail-closed direction is the part worth copying.
_PENDING_CONFIRMATIONS: dict[str, dict[str, Any]] = {}

# Tasks that stopped at the token-budget guard and ended their turn asking the
# customer whether to continue, keyed by customer_id: {run_id, goal,
# pending_request}. The harness reads that yes/no itself (same rule as
# `_PENDING_CONFIRMATIONS`): a "yes" is the human buying another grant, not the
# model deciding it deserves one. The entry is what lets the next turn resume
# the SAME run instead of starting a fresh task with a fresh meter.
_AWAITING_CONTINUE: dict[str, dict[str, str]] = {}


def get_agent(customer_id: str, profile=PROFILE) -> Agent:
    if customer_id not in _AGENTS:
        _AGENTS[customer_id] = build_agent(profile=profile, customer_id=customer_id)
    return _AGENTS[customer_id]


# Dedicated catalog agents for /api/tools — one per (skills_enabled,
# episodic_enabled) combo so the drawer reflects the live tool set for
# the current UI toggle state. At most 4 cached instances.
_CATALOG_AGENTS: dict[tuple[bool, bool], Agent] = {}


def _plan_intent(plan: str) -> str | None:
    match = re.search(r"^intent:\s*(.+)$", plan, flags=re.IGNORECASE | re.MULTILINE)
    return match.group(1).strip() if match else None


def _get_catalog_agent(skills_enabled: bool, episodic_enabled: bool) -> Agent:
    key = (skills_enabled, episodic_enabled)
    if key not in _CATALOG_AGENTS:
        profile = apply_overrides(
            PROFILE,
            skills_enabled=skills_enabled,
            episodic_enabled=episodic_enabled,
        )
        _CATALOG_AGENTS[key] = build_agent(profile=profile, customer_id="_catalog")
    return _CATALOG_AGENTS[key]


# ---------------------------------------------------------------------------
# SSE event extraction from Strands' stream_async()
# ---------------------------------------------------------------------------


def _truncate(value: Any, n: int = 60) -> str:
    s = "" if value is None else str(value)
    return s if len(s) <= n else s[: n - 1] + "…"


def _tool_names(specs: list[dict]) -> list[str]:
    return [str(spec.get("toolSpec", spec).get("name", "?")) for spec in specs]


def _summarize_args(name: str, args: dict | None) -> str:
    if not isinstance(args, dict):
        return _truncate(args)
    if name == "lookup_customer":
        return args.get("customer_id", "?")
    if name == "get_order":
        return args.get("order_id", "?")
    if name in {"get_customer_orders", "get_open_tickets", "get_refund_history"}:
        return args.get("customer_id", "?")
    if name == "check_policy":
        questions = args.get("policy_questions") or []
        count = len(questions) if isinstance(questions, list) else 1
        return f"{count} policy question{'' if count == 1 else 's'}"
    if name == "issue_refund":
        pct = args.get("refund_percentage")
        pct_str = f"{pct:.0%}" if isinstance(pct, (int, float)) else "?"
        return (
            f"{args.get('order_id', '?')}, "
            f"{pct_str}, "
            f"{args.get('reason_code', '?')}, "
            f'"{_truncate(args.get("reason", ""), 30)}"'
        )
    if name == "escalate_to_human":
        return (
            f"priority={args.get('priority', 'normal')}, "
            f'"{_truncate(args.get("reason", ""), 40)}"'
        )
    if name == "skills":
        return args.get("skill_name", "?")
    return ", ".join(f"{k}={_truncate(v, 25)}" for k, v in args.items())


def _summarize_result(name: str, result: Any) -> tuple[str, bool]:
    """Return (summary, is_error)."""
    if isinstance(result, dict) and "error" in result:
        code = result.get("code")
        detail = result.get("detail") or result.get("error")
        remediation = result.get("remediation")
        bits = [str(code)] if code else []
        bits.append(_truncate(detail, 50))
        if remediation:
            bits.append(f"→ {remediation}")
        return "err: " + " ".join(bits), True
    if name == "lookup_customer" and isinstance(result, dict):
        return f"{result.get('name', '?')}, {result.get('tier', '?')}", False
    if name == "get_order" and isinstance(result, dict):
        status = result.get("status", "?")
        total = result.get("total_usd", "?")
        late = result.get("delivery_days_late", 0)
        late_str = f", {late}d late" if late else ""
        # Whether photo evidence is on file decides the damaged-item refund, so
        # surface it in the one-line trace rather than burying it in the body.
        photo_str = ""
        if result.get("damaged"):
            photos = result.get("damage_photos") or []
            photo_str = f", {len(photos)} photo(s)" if photos else ", no photos"
        return f"{status}, ${total}{late_str}{photo_str}", False
    if name == "check_policy" and isinstance(result, dict):
        policy_ids = [
            str(item.get("id"))
            for item in result.get("applicable_policies", [])
            if isinstance(item, dict) and item.get("id")
        ]
        policies = ", ".join(policy_ids) or "no matching policy"
        steps = result.get("required_steps") or []
        return (
            f"{result.get('decision', 'unknown')} · {policies} · "
            f"{len(steps)} next step{'' if len(steps) == 1 else 's'}"
        ), False
    if name == "issue_refund" and isinstance(result, dict):
        return f"ok {result.get('ref', '')}, ${result.get('amount_usd', '?')}", False
    if name == "escalate_to_human" and isinstance(result, dict):
        return f"ok {result.get('ticket_id', '')} ({result.get('priority', 'normal')})", False
    if isinstance(result, list):
        return f"[{len(result)} items]", False
    if isinstance(result, str):
        return _truncate(result, 70), False
    if isinstance(result, dict):
        if result.get("ok"):
            ref = result.get("ref") or result.get("ticket_id") or ""
            return f"ok {ref}".strip(), False
        return _truncate(result, 70), False
    return _truncate(result, 70), False


def _extract_tool_result_body(block: dict) -> Any:
    tr = block.get("toolResult") or block.get("tool_result")
    if not isinstance(tr, dict):
        return None
    body = tr.get("content")
    if isinstance(body, list) and body:
        head = body[0]
        if isinstance(head, dict):
            body = head.get("text") or head.get("json") or head
    if isinstance(body, str):
        try:
            return json.loads(body)
        except (ValueError, TypeError):
            return body
    return body


def _drop_marker_message(agent: Agent) -> None:
    """Remove the guard's cancellation marker from the agent's history."""
    messages = getattr(agent, "messages", None)
    if not messages:
        return
    last = messages[-1]
    if not isinstance(last, dict) or last.get("role") != "assistant":
        return
    content = last.get("content")
    text = ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = " ".join(
            str(b.get("text", "")) for b in content if isinstance(b, dict)
        )
    if BUDGET_STOP_MARKER in text:
        messages.pop()


async def _run_agent_stream(
    agent: Agent, prompt, *, run_state, display_prompt=None, resuming: bool = False
):
    """Yield SSE events for one agent turn.

    `prompt` is normally the user message. When the previous turn ended on a
    human-confirmation interrupt it is instead a list of `interruptResponse`
    blocks — Strands' resume payload — and `display_prompt` carries the text
    the customer actually typed, for the trace.

    `prompt=None` resumes the loop from the conversation as it stands, adding
    nothing to it. That is how a task paused at the token guard continues: the
    session memory already ends with the observation the model was about to
    read, so the next model call is simply the one the guard withheld.
    """
    if prompt is None:
        agent._current_user_message_token_estimate = 0
    elif isinstance(prompt, str):
        agent._current_user_message_token_estimate = max(1, (len(prompt) + 3) // 4)
    else:
        serialized_prompt = json.dumps(prompt, default=str)
        agent._current_user_message_token_estimate = max(
            1, (len(serialized_prompt) + 3) // 4
        )
    # Echo the exact message handed to the LLM. engineered doesn't frame anything
    # at the message layer (identity is bound by CustomerIdBindingHook at
    # the tool layer), so this is just the raw prompt.
    # `resuming` carries no customer message: a confirmation answer is a
    # decision on a parked tool call, not a new thing said to the agent, and
    # the conversation the model reads is unchanged by it. Echoing one here
    # would put words in the customer's mouth in the trace.
    if prompt is not None and not resuming:
        yield {
            "event": "user_message",
            "data": json.dumps(
                {"content": display_prompt if display_prompt is not None else prompt}
            ),
        }

    # `system_prompt` is emitted AFTER the stream begins so the AgentSkills
    # plugin (which injects its skills catalog via a BeforeInvocationEvent
    # hook) has had a chance to update `agent.system_prompt` first. If we
    # emitted it before stream_async, the drawer in the UI would show the
    # bare prompt without the "Available skills" catalog.
    system_prompt_emitted = False
    # A confirmation resume is the same turn continuing, which is why
    # `RunStore.start(continue_turn=True)` carries `model_call_usage` across the
    # pause. Clearing the snapshots here would restart the context chart's
    # numbering at 1 while the usage counter kept climbing, and the console
    # draws both halves of that one chart from these numbers. Keep the
    # snapshots and pick up counting where the pre-pause half stopped.
    # A budget-grant continuation is a NEW turn and arrives with
    # `resuming=False`, so it still gets a clean chart.
    if not resuming:
        agent._context_trace_snapshots = []
    emitted_contexts = len(getattr(agent, "_context_trace_snapshots", []))
    # Compactions are appended by the context pipeline from inside the event
    # loop (see context_compaction.py), so they are drained alongside the
    # context snapshots rather than emitted from the hook itself.
    emitted_compactions = len(run_state.compactions)

    # tool_use_id -> {name, args}. Kept on the AGENT, not this coroutine, because
    # a HumanConfirmationHook interrupt splits one model cycle across two /api/run
    # calls: the assistant message carrying the toolUse blocks is streamed in the
    # first call, and the toolResult message for the WHOLE batch (including the
    # siblings that ran to completion alongside the interrupted one) only lands
    # after the resume. With a per-stream dict those results find no slot and get
    # dropped — invisible in the trace and, worse, never fed to
    # `record_observation`. Strands itself keeps them (PendingToolExecution
    # .completed_tool_results), so the model always saw them; this is the harness
    # catching up.
    pending: dict[str, dict] = getattr(agent, "_pending_tool_calls", None) or {}
    if not resuming and not getattr(agent._interrupt_state, "activated", False):
        # Not a resume and nothing parked: anything left over is from a turn
        # that was abandoned (reset / fail-closed). Drop it rather than pairing
        # this turn's results with last turn's calls.
        pending = {}
    agent._pending_tool_calls = pending
    interrupts: list[dict] = []

    # Resume: the replayed assistant message never re-enters the event stream
    # (the event loop short-circuits the model call), so re-announce the calls
    # still in flight to keep this turn's trace self-contained.
    if resuming and pending:
        for tu_id, slot in pending.items():
            yield {
                "event": "tool_call",
                "data": json.dumps(
                    {
                        "tool_use_id": tu_id,
                        "name": slot["name"],
                        "args": slot["args"],
                        "args_summary": _summarize_args(slot["name"], slot["args"]),
                    }
                ),
            }

    async for event in agent.stream_async(prompt):
        while emitted_compactions < len(run_state.compactions):
            entry = run_state.compactions[emitted_compactions]
            emitted_compactions += 1
            yield {
                "event": "context_compacted",
                "data": json.dumps(
                    {
                        "run_id": run_state.run_id,
                        "summary": (
                            f"Context compacted before model call {entry['call']}: "
                            f"{entry['messages_summarized']} messages summarized, "
                            f"{entry['before_tokens']:,} → {entry['after_tokens']:,} tokens"
                        ),
                        **entry,
                    }
                ),
            }
        snapshots = getattr(agent, "_context_trace_snapshots", [])
        while emitted_contexts < len(snapshots):
            snapshot = snapshots[emitted_contexts]
            emitted_contexts += 1
            record_iteration(run_state)
            yield {
                "event": "context_iteration",
                "data": json.dumps(
                    {
                        "run_id": run_state.run_id,
                        "summary": f"Actual context sent to model call {emitted_contexts}",
                        "model_call": emitted_contexts,
                        **snapshot,
                    }
                ),
            }
        if not system_prompt_emitted:
            yield {
                "event": "system_prompt",
                "data": json.dumps({"content": agent.system_prompt or ""}),
            }
            system_prompt_emitted = True

        if not isinstance(event, dict):
            continue

        # A hook asked for human input (HumanConfirmationHook). The event loop
        # has already stopped and the tool never ran; collect what the human
        # has to answer so the caller can put the question to them.
        result = event.get("result")
        if getattr(result, "stop_reason", None) == "interrupt":
            for interrupt in result.interrupts or []:
                reason = interrupt.reason if isinstance(interrupt.reason, dict) else {}
                interrupts.append(
                    {
                        "id": interrupt.id,
                        "name": interrupt.name,
                        "tool": reason.get("tool", ""),
                        "tool_use_id": reason.get("tool_use_id", ""),
                        "args": reason.get("args", {}),
                        "question": reason.get("question", ""),
                        "card": reason.get("card", {}),
                    }
                )

        # Streaming text delta for the final reply
        delta = event.get("data")
        if isinstance(delta, str):
            yield {"event": "text_delta", "data": json.dumps({"delta": delta})}

        msg = event.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        role = msg.get("role")

        for block in content:
            if not isinstance(block, dict):
                continue

            if role == "assistant":
                tu = block.get("toolUse") or block.get("tool_use")
                if isinstance(tu, dict):
                    tu_id = tu.get("toolUseId") or tu.get("id")
                    name = tu.get("name")
                    if not tu_id or not name:
                        continue
                    args = tu.get("input") or {}
                    pending[tu_id] = {"name": name, "args": args}
                    yield {
                        "event": "tool_call",
                        "data": json.dumps(
                            {
                                "tool_use_id": tu_id,
                                "name": name,
                                "args": args,
                                "args_summary": _summarize_args(name, args),
                            }
                        ),
                    }
            elif role == "user":
                tr = block.get("toolResult") or block.get("tool_result")
                if not isinstance(tr, dict):
                    continue
                tu_id = tr.get("toolUseId") or tr.get("tool_use_id") or tr.get("id")
                slot = pending.pop(tu_id, None)
                if not slot:
                    continue
                body = _extract_tool_result_body(block)
                summary, is_err = _summarize_result(slot["name"], body)
                # The protocol carries its own failure flag. Honour it so an
                # unstructured failure (a raw exception the SDK stringified)
                # is flagged here rather than left to the UI to sniff for.
                is_err = is_err or tr.get("status") == "error"
                yield {
                    "event": "tool_result",
                    "data": json.dumps(
                        {
                            "tool_use_id": tu_id,
                            "name": slot["name"],
                            "result": body,
                            "result_summary": summary,
                            "is_error": is_err,
                        }
                    ),
                }

    # Fallback: if the stream yielded zero events (rare — error before any
    # output), emit the (now-injected) system_prompt so the drawer renders.
    if not system_prompt_emitted:
        yield {
            "event": "system_prompt",
            "data": json.dumps({"content": agent.system_prompt or ""}),
        }

    while emitted_compactions < len(run_state.compactions):
        entry = run_state.compactions[emitted_compactions]
        emitted_compactions += 1
        yield {
            "event": "context_compacted",
            "data": json.dumps(
                {
                    "run_id": run_state.run_id,
                    "summary": (
                        f"Context compacted before model call {entry['call']}: "
                        f"{entry['messages_summarized']} messages summarized, "
                        f"{entry['before_tokens']:,} → {entry['after_tokens']:,} tokens"
                    ),
                    **entry,
                }
            ),
        }
    snapshots = getattr(agent, "_context_trace_snapshots", [])
    while emitted_contexts < len(snapshots):
        snapshot = snapshots[emitted_contexts]
        emitted_contexts += 1
        # Same bookkeeping the in-stream drain does. A snapshot that only
        # surfaces after the stream closes is still an iteration; without this
        # `iteration_count` undercounts the `context_iteration` events.
        record_iteration(run_state)
        yield {
            "event": "context_iteration",
            "data": json.dumps(
                {
                    "run_id": run_state.run_id,
                    "summary": f"Actual context sent to model call {emitted_contexts}",
                    "model_call": emitted_contexts,
                    **snapshot,
                }
            ),
        }

    if interrupts:
        # No final reply this turn: the loop is parked mid-tool-call waiting on
        # a human. `done` is deliberately NOT emitted here — the caller decides
        # what the customer sees and closes the stream itself.
        yield {"event": "interrupt", "data": json.dumps({"interrupts": interrupts})}
        return

    # Final reply — pull the last assistant message
    messages = getattr(agent, "messages", []) or []
    final_reply = ""
    for m in reversed(messages):
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        c = m.get("content")
        if isinstance(c, str):
            final_reply = c
            break
        if isinstance(c, list):
            for blk in c:
                if isinstance(blk, dict) and blk.get("text"):
                    final_reply = blk["text"]
                    break
            if final_reply:
                break

    yield {
        "event": "done",
        "data": json.dumps(
            {"final_reply": final_reply, "usage": usage_payload(run_state)}
        ),
    }


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------


class RunRequest(BaseModel):
    prompt: str
    customer_id: str
    model: str | None = None  # Per-request override; falls back to PROFILE.model.
    # UI toggles for engineered features. Null falls back to profile defaults.
    # `skills_enabled`, `episodic_enabled`, and `hitl_enabled` require a
    # session reset (the agent is cached and its prompt, tools, plugins, and
    # hooks are baked at build time), so the frontend calls /api/reset before
    # sending a request with new values. `planner_enabled` is a per-request decision (the
    # planner is a separate LLM call, not part of the agent build), so it
    # can flip freely without a reset.
    skills_enabled: bool | None = None
    episodic_enabled: bool | None = None
    planner_enabled: bool | None = None
    # Optional post-turn LLM reviews. Off unless the presenter enables them.
    evaluation_enabled: bool = False
    run_id: str | None = None
    token_budget: int | None = None
    # Auto-compaction line, in projected input tokens for the next model call.
    # Null or 0 leaves context to grow. Like `token_budget` and unlike
    # `skills_enabled`, this is a live dial: it applies to the next model call
    # of the session in flight, with no reset.
    compact_at: int | None = None
    # Build-time human-approval gate for customer-visible writes. Changing it
    # requires the same reset/rebuild as Skills and episodic memory.
    hitl_enabled: bool | None = None
    # One-shot demo fault: the next refund service call commits the write,
    # then times out before acknowledging it.
    refund_service_timeout: bool = False
    # Answers to the two pauses the console renders inline, both structured
    # rather than read out of prose. `budget_grant` answers
    # `budget_grant_required`: the same run resumes with its ceiling raised.
    # `confirm` answers `write_confirmation_required`: the queued write runs
    # or is cancelled, and the parked loop resumes either way.
    budget_grant: bool | None = None
    confirm: bool | None = None
    # One decision per parked write, keyed by interrupt id. Parallel writes
    # park as separate interrupts, so the console answers them separately and
    # a customer can approve one address change while refusing the other.
    # `confirm` remains the all-or-nothing answer for a single queued write.
    confirm_decisions: dict[str, bool] | None = None


app = FastAPI(title="cs_agent_engineered", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1):51(7[0-9]|8[0-9])",
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "agent": "cs_agent_engineered", "agent_id": PROFILE.agent_id}


def _catalog_system_prompt(agent: Agent) -> str:
    """`agent.system_prompt` with the skills catalog the model would actually see.

    AgentSkills appends its `<available_skills>` XML on BeforeInvocationEvent,
    so a catalog agent — built to answer this endpoint and never invoked — stops
    at the bare "## Available skills" header. Reading the prompt off it would
    show the UI a section the model never gets an empty version of. Generate the
    same XML here instead; the skills were loaded back in `build_agent`, so this
    is a formatting call, not I/O.

    Built as a local string rather than assigned to `agent.system_prompt`: the
    catalog agents are cached per toggle combo, and the plugin's de-duplication
    only tracks blocks IT injected, so writing back would stack a fresh copy on
    every request.
    """
    prompt = agent.system_prompt or ""
    plugin = getattr(agent, "_skills_plugin", None)
    if plugin is None:
        return prompt
    return f"{prompt}\n\n{plugin._generate_skills_xml(agent)}"


@app.get("/api/tools")
def tools_catalog(
    skills_enabled: bool | None = None,
    episodic_enabled: bool | None = None,
) -> dict[str, Any]:
    """List the agent's tools (MCP specs + local episodic-memory tools + the
    AgentSkills loader). The toggle flags scope the result to the live tool
    set for that combo — the UI refetches whenever the toggles flip so the
    drawer matches what the agent actually has."""
    effective_skills = (
        skills_enabled if skills_enabled is not None else bool(PROFILE.skills_dir)
    )
    effective_episodic = (
        episodic_enabled
        if episodic_enabled is not None
        else PROFILE.memory.episodic.enabled
    )
    agent = _get_catalog_agent(effective_skills, effective_episodic)
    return {
        "tools": agent.tool_registry.get_all_tool_specs(),
        # The rendered system prompt for this toggle combo (agent-profile.yaml
        # with identity and caps substituted in). Served from the catalog so
        # the UI can show it without waiting for a run to stream it back.
        "system_prompt": _catalog_system_prompt(agent),
    }


@app.get("/api/memory")
def get_memory(customer_id: str) -> dict[str, Any]:
    """Return the episodic-memory file contents for a customer.

    The endpoint is unconditional so the UI can show "empty" vs "missing"
    states. The web frontend gates whether to surface the drawer at all
    behind the engineered episodic_enabled toggle."""
    from agent.memory import InvalidCustomerId, _path, load

    try:
        exists = _path(customer_id).exists()
    except InvalidCustomerId:
        # `customer_id` is untrusted query input; _path refuses anything that
        # would escape the memory directory. Answer 400 rather than 500.
        raise HTTPException(status_code=400, detail="invalid customer_id")
    return {
        "customer_id": customer_id,
        "exists": exists,
        "content": load(customer_id),
    }


class BudgetDecisionRequest(BaseModel):
    customer_id: str
    run_id: str | None = None


@app.post("/api/budget_stop")
def budget_stop(req: BudgetDecisionRequest) -> dict[str, Any]:
    """Record that the customer chose Stop on a paused task.

    No agent runs: the point is only to drop the pause so a later message is
    unambiguously a new request. The paused run keeps its state for the trace.
    """
    pending = _AWAITING_CONTINUE.pop(req.customer_id, None)
    run_state = _RUNS.get(pending["run_id"]) if pending else None
    if run_state is not None:
        run_state.status = "stopped"
        run_state.next_loop_state = "STOP"
        run_state.exit_reason = "STOPPED_AT_BUDGET_GUARD"
        run_state.resume_condition = None
    return {"ok": True, "stopped": bool(pending)}


@app.post("/api/run")
async def run(req: RunRequest):
    # Apply UI toggle overrides to a per-request profile copy. From here on
    # the profile IS the source of truth — no separate flag plumbing.
    effective_profile = apply_overrides(
        PROFILE,
        skills_enabled=req.skills_enabled,
        episodic_enabled=req.episodic_enabled,
        planner_enabled=req.planner_enabled,
        hitl_enabled=req.hitl_enabled,
    )
    agent = get_agent(req.customer_id, effective_profile)
    # The fault is one-shot and disk-backed, so it outlives the turn that armed
    # it unless the refund tool consumes it. Disarm whenever the toggle is off
    # so a primed fault can never surface on a later turn the console shows as
    # fault-free.
    if req.refund_service_timeout:
        arm_fault(PROFILE.agent_id, "refund_service_timeout")
    else:
        disarm_fault(PROFILE.agent_id, "refund_service_timeout")

    # Is this message the answer to a confirmation the agent is parked on?
    # If so it is not a new turn at all: it unblocks the tool call that is
    # still sitting inside the previous turn's event loop. The console answers
    # with the same inline control the budget pause uses (`confirm`); a typed
    # message is read by the harness with `is_affirmative`. Either way the
    # MODEL never decides that it was approved, and anything that is not a
    # clear yes is a no — which is why prose is still safe to accept here and
    # is not for the budget pause, where the fail-closed reading of an
    # unrelated message is "this is a new request".
    pending = _PENDING_CONFIRMATIONS.pop(req.customer_id, None)
    if pending and not getattr(agent._interrupt_state, "activated", False):
        # The agent was rebuilt (reset / end_session) or never parked: there is
        # nothing to resume, so the queued write is simply gone. Fail closed and
        # treat the message as an ordinary new turn.
        pending = None
    resume_payload = None
    approved = False
    decisions: dict[str, bool] = {}
    if pending:
        per_write = req.confirm_decisions or {}

        def _decide(interrupt_id: str) -> bool:
            # Per-write answer first, then the single-write control, then the
            # typed reply. Every path is read by the harness and defaults to
            # "not approved" — an answer that is missing or unclear is a no.
            if interrupt_id in per_write:
                return bool(per_write[interrupt_id])
            if req.confirm is not None:
                return bool(req.confirm)
            return is_affirmative(req.prompt)

        decisions = {item["id"]: _decide(item["id"]) for item in pending["interrupts"]}
        # The turn as a whole counts as approved only if every queued write was.
        approved = all(decisions.values())
        resume_payload = [
            {
                "interruptResponse": {
                    "interruptId": item["id"],
                    "response": {
                        "approved": decisions[item["id"]],
                        "customer_reply": req.prompt,
                    },
                }
            }
            for item in pending["interrupts"]
        ]

    # Did the previous turn pause at the token guard? Then the console's
    # inline Continue/Stop answer arrives here as `budget_grant`. It is a
    # decision, not prose: the model never gets to rule that it has been
    # granted more budget, and the harness never has to guess what a message
    # meant. Anything typed into the composer instead is a NEW request, which
    # drops the pause — an ambiguous "yes, and also cancel my order" must not
    # silently resume a task the customer may have moved on from.
    paused = _AWAITING_CONTINUE.pop(req.customer_id, None)
    awaiting_continue = paused["run_id"] if paused else None
    continue_approved = bool(paused) and bool(req.budget_grant)
    # What the task is actually about. A continuation message ("Yes, continue.")
    # is an answer, not the request, so the original one is carried forward and
    # is what the agent resumes against and what any later wrap-up summarises.
    task_request = (
        (paused or {}).get("pending_request") or req.prompt
        if continue_approved
        else (pending or {}).get("prompt") or req.prompt
    )

    # An approved continuation is the SAME task: resume its run so the meter
    # and tool history carry over, then buy it
    # one more grant. A new message is a new task and starts from zero.
    resume_run_id = awaiting_continue if continue_approved else req.run_id
    run_state = _RUNS.start(
        run_id=resume_run_id,
        customer_id=req.customer_id,
        goal=req.prompt.strip() if not continue_approved else (paused or {}).get(
            "goal", req.prompt.strip()
        ),
        token_budget=req.token_budget,
        compact_at=req.compact_at,
        resume=continue_approved,
        # A confirmation answer resumes the turn the customer is already
        # looking at — the console streams what follows into that same card —
        # so its counters and per-model-call usage must carry on rather than
        # restart. A budget grant is a fresh card and does start a new turn.
        continue_turn=resume_payload is not None,
    )
    granted_ceiling = grant_budget(run_state) if continue_approved else None
    if not run_state.success_criteria:
        run_state.success_criteria = ["response_produced"]
    agent._active_run_state = run_state
    # Apply the per-request model choice by mutating the cached agent's
    # model. Conversation memory (`agent.messages`) is untouched, so the
    # same chat continues with a different backend.
    agent.model = OpenAIModel(model_id=req.model or PROFILE.model)

    # frame_prompt lives in agent/core.py — parallels cs_agent_first_cut's
    # `frame_prompt`. engineered's version is a no-op: customer_id is bound by
    # CustomerIdBindingHook, not inlined into the message.
    framed_prompt = frame_prompt(req.customer_id, req.prompt)

    # Planning layer: a small non-streaming LLM call (no tools) that reads
    # the customer message and produces a <plan> block scoping the turn —
    # intent, approach, info_needed, skills, policies. Prepended to the
    # user message so the main agent reads intent BEFORE picking tools.
    # Separating intent recognition from tool selection is what stops the
    # main agent from pattern-matching on "late order → refund" and missing
    # what the customer actually wants. Implemented in `planner.py` at the
    # lab root and shared with cs_agent_first_cut.
    #
    # Gated by `profile.planner.enabled` so the UI toggle / YAML flag can
    # flip it off mid-demo to show the regression. The planner sees the
    # LIVE tool surface — tool specs come straight from the cached agent's
    # registry, so adding / removing an MCP tool is auto-reflected in the
    # planner's catalogue. No drift.
    plan = ""
    if (
        effective_profile.planner.enabled
        # A resume carries no new intent to plan — the plan that produced the
        # queued tool call is still the one in force.
        and resume_payload is None
        # Same for a granted continuation: the message is a decision about
        # budget, not a request, and the loop resumes on its own plan. Planning
        # it would spend tokens reading "Continue" as if it were a new problem.
        and not continue_approved
    ):
        planner_model = req.model or PROFILE.model
        try:
            tools_catalogue = format_tool_specs(
                agent.tool_registry.get_all_tool_specs()
            )
            # Only let the planner suggest skills if the main agent
            # actually has the AgentSkills plugin loaded — otherwise the
            # plan would point at procedures the agent can't load. When
            # enabled, pass engineered's skills directory in so the planner can
            # read the live SKILL.md frontmatter (it lives in cs_agent_engineered/,
            # which the lab-root planner.py doesn't know about by default).
            skills_dir = effective_profile.skills_dir
            skills_cat = (
                skills_catalogue_from_dir(ROOT / skills_dir if not Path(skills_dir).is_absolute() else skills_dir)
                if skills_dir
                else None
            )
            plan, planner_usage = await plan_for_prompt(
                req.prompt,
                model=planner_model,
                tools_catalogue=tools_catalogue,
                skills_catalogue=skills_cat,
                skills_enabled=bool(skills_dir),
            )
            add_auxiliary(run_state, planner_usage)
        except Exception:  # noqa: BLE001
            # Planner failures (timeout, rate limit, tool-registry hiccup)
            # shouldn't take the turn down — fall back to the unplanned
            # prompt and let the main agent handle the message as it would
            # have before.
            log.exception("planner call failed; proceeding without a plan")
            plan = ""

    # Episodic memory injection: on the FIRST turn after a fresh Agent build
    # (e.g. right after /api/end_session has cleared the cache), prepend the
    # customer's stored notes inside `<episodic_memory>` tags. We deliberately
    # put memory at the user-message level — not the system prompt — so the
    # LLM weights it as session context, and so the protocol text in the
    # system prompt doesn't drown out the actual notes.
    loaded_memory = ""
    if (
        not agent.messages
        and effective_profile.memory.episodic.enabled
        and resume_payload is None
    ):
        from agent.memory import load as load_memory
        loaded_memory = load_memory(req.customer_id)
        framed_prompt = prepend_memory(
            req.customer_id,
            framed_prompt,
            compact_threshold=effective_profile.memory.episodic.compact_threshold,
        )

    # Plan goes above the memory + message, since intent is the first thing
    # the model should read. Order in the final user message:
    #   <plan>…</plan>
    #   <episodic_memory>…</episodic_memory>   (only on the first turn of a session)
    #   <customer's actual message>
    if plan:
        framed_prompt = f"{plan}\n\n{framed_prompt}"
        intent = _plan_intent(plan)
        if intent:
            run_state.goal = intent

    async def generator():
        tools = agent.tool_registry.get_all_tool_specs()
        snapshot = context_snapshot(
            variant="engineered",
            prompt=framed_prompt,
            system_prompt=agent.system_prompt or "",
            message_count=len(getattr(agent, "messages", []) or []),
            tool_names=_tool_names(tools),
            plan=plan,
            memory=loaded_memory,
            execution_state_selected=False,
        )
        yield loop_event(
            "context_build",
            run_state,
            "Initial model context assembled from visible runtime inputs",
            context=snapshot,
            provenance={
                "goal": "planner intent" if plan else "customer request",
                "success_criteria": "base response contract; extended only when a Skill is loaded",
                "scenario_metadata_received": False,
                "run_state_in_model_context": False,
            },
        )
        if loaded_memory.strip():
            yield loop_event(
                "memory_retrieval",
                run_state,
                "Customer memory selected into this turn context",
                customer_id=req.customer_id,
                content=loaded_memory,
            )
        # Emit the plan as its own SSE event so the trace UI can render it
        # as a distinct step before tool calls start. Frontend that doesn't
        # know about this event type will silently drop it.
        if plan:
            yield {"event": "plan", "data": json.dumps({"content": plan})}
        if awaiting_continue:
            yield loop_event(
                "human_approval",
                run_state,
                "Customer chose Continue; the paused loop resumes with another grant"
                if continue_approved
                else "Customer did not continue the paused task",
                decision="APPROVED" if continue_approved else "DENIED",
                approved=continue_approved,
                customer_reply=req.prompt,
                paused_run_id=awaiting_continue,
                grant_tokens=run_state.token_budget if continue_approved else 0,
                token_ceiling=granted_ceiling or token_ceiling(run_state),
                total_tokens_already_used=loop_tokens_used(run_state),
                answered_by="console continue/stop control",
                decided_by="harness",
            )
        if resume_payload is not None:
            # Into the trajectory as well as the trace: the evaluators read
            # `tool_history`, and a write whose consent is invisible there
            # cannot be told apart from one that was never asked about.
            for item in pending["interrupts"]:
                record_human_decision(
                    run_state,
                    tool_name=item["tool"],
                    approved=bool(decisions.get(item["id"])),
                    tool_use_id=item.get("tool_use_id") or None,
                    customer_reply=req.prompt,
                )
            yield loop_event(
                "human_approval",
                run_state,
                "Customer approved every paused write"
                if approved
                else "Customer did not approve every paused write",
                decision="APPROVED" if approved else "DENIED",
                approved=approved,
                customer_reply=req.prompt,
                answered_by=(
                    "console proceed/reject control"
                    if req.confirm is not None or req.confirm_decisions
                    else "customer message"
                ),
                # Per write, not per turn: a batch of parallel writes can come
                # back part approved, and the trace has to show which.
                awaiting=[
                    {
                        "tool": item["tool"],
                        "args": item["args"],
                        "decision": (
                            "APPROVED" if decisions.get(item["id"]) else "DENIED"
                        ),
                    }
                    for item in pending["interrupts"]
                ],
                decided_by="harness",
            )
        try:
            # A granted continuation adds NOTHING to the conversation: the loop
            # picks up from the session memory it paused on. Anything injected
            # here — a "the customer said yes" note, the wrap-up text — would
            # make the model re-orient instead of simply taking its next step.
            if resume_payload is not None:
                turn_prompt = resume_payload
            elif continue_approved:
                turn_prompt = None
            else:
                turn_prompt = framed_prompt
            skill_calls: dict[str, str] = {}
            proposed_reply = ""
            pending_interrupts: list[dict] = []
            async for ev in _run_agent_stream(
                agent,
                turn_prompt,
                run_state=run_state,
                display_prompt=framed_prompt,
                resuming=resume_payload is not None,
            ):
                event_type = ev.get("event")
                if event_type == "tool_call":
                    body = json.loads(ev.get("data", "{}"))
                    name = body.get("name", "tool")
                    yield loop_event(
                        "llm_decision",
                        run_state,
                        f"Invoke {name}",
                        selected_action=name,
                    )
                    if name == "skills":
                        tool_use_id = body.get("tool_use_id")
                        skill_name = body.get("args", {}).get("skill_name", "")
                        if tool_use_id and skill_name:
                            skill_calls[tool_use_id] = skill_name
                        yield loop_event(
                            "skill_load",
                            run_state,
                            f"Model selected procedure {skill_name or '?'}",
                            skill_name=skill_name,
                            selection_source="model tool call",
                        )
                if event_type == "interrupt":
                    pending_interrupts = json.loads(ev.get("data", "{}")).get(
                        "interrupts", []
                    )
                    continue
                # The reply is buffered rather than streamed so the harness
                # can attach its evaluation to the same frame the customer
                # sees, instead of the verdict arriving after the text.
                if event_type not in {"text_delta", "done"}:
                    yield ev
                if event_type == "tool_result":
                    body = json.loads(ev.get("data", "{}"))
                    record_observation(
                        run_state,
                        tool_name=body.get("name", "tool"),
                        observation=body.get("result"),
                        is_error=bool(body.get("is_error")),
                        tool_use_id=body.get("tool_use_id") or None,
                    )
                    # Read the structured field, not the stringified result:
                    # an escalation `reason` or a policy brief quoting the
                    # remediation also contains the word "service_timeout".
                    # The lab's own lesson — the error is structured, so match
                    # it structurally.
                    tool_result = body.get("result")
                    if (
                        isinstance(tool_result, dict)
                        and tool_result.get("error") == "service_timeout"
                    ):
                        yield loop_event(
                            "recovery",
                            run_state,
                            "Refund service did not acknowledge the write; outcome unknown",
                            fault="refund_service_timeout",
                            decision="verify_then_escalate",
                        )
                    if body.get("name") == "skills":
                        skill_name = skill_calls.get(body.get("tool_use_id", ""), "")
                        contract = None
                        if skill_name and effective_profile.skills_dir:
                            skills_root = Path(effective_profile.skills_dir)
                            if not skills_root.is_absolute():
                                skills_root = ROOT / skills_root
                            contract = load_skill_contract(skills_root, skill_name)
                        if contract and not body.get("is_error"):
                            apply_task_contract(
                                run_state,
                                contract,
                                source=f"skill:{skill_name}",
                            )
                            yield loop_event(
                                "task_contract",
                                run_state,
                                f"Harness registered the contract supplied by {skill_name}",
                                source=f"skill:{skill_name}",
                                contract=contract,
                                state=run_state.dump(),
                            )
                        yield loop_event(
                            "skill_load",
                            run_state,
                            "Selected procedure added to model context",
                            skill_name=skill_name,
                            procedural_context=body.get("result"),
                        )
                    # Same rule: a memory write is the two tools that write
                    # memory, not any result whose text happens to carry their
                    # response keys.
                    if body.get("name") in {"append_memory", "compact_memory"} and not body.get(
                        "is_error"
                    ):
                        yield loop_event(
                            "memory_write",
                            run_state,
                            "Selective episodic note persisted",
                            observation=body.get("result"),
                        )
                    yield loop_event(
                        "state_transition",
                        run_state,
                        "Execution progress updated from observation",
                        state=run_state.dump(),
                    )
                    yield loop_event(
                        "context_update",
                        run_state,
                        f"The {body.get('name', 'tool')} observation enters the next model call",
                        added_message={
                            "role": "tool",
                            "tool": body.get("name", "tool"),
                            "content": body.get("result"),
                        },
                        source="tool observation",
                        execution_state_injected=False,
                    )
                    yield loop_event(
                        "loop_decision",
                        run_state,
                        "Continue with the updated progress",
                        decision="CONTINUE",
                    )
                if event_type == "done":
                    proposed_reply = json.loads(ev.get("data", "{}")).get(
                        "final_reply", ""
                    )

            if pending_interrupts:
                # A write tool is queued behind a human's yes. The turn ends
                # with the question, and the loop stays parked inside the
                # agent's interrupt state until the next /api/run resumes it.
                _PENDING_CONFIRMATIONS[req.customer_id] = {
                    "interrupts": pending_interrupts,
                    "prompt": req.prompt,
                }
                # One line for the trace and for any client that cannot
                # render the structured cards. The console does not show this:
                # it renders `awaiting`, one consent card per queued write.
                questions = [
                    item["question"] for item in pending_interrupts if item["question"]
                ]
                question = " ".join(questions)
                run_state.status = "waiting"
                run_state.next_loop_state = "ASK"
                run_state.exit_reason = "AWAITING_HUMAN_CONFIRMATION"
                run_state.pending_request = req.prompt
                run_state.resume_condition = "customer answers the confirmation question"
                # Same `pause` contract as the budget guard, so the console
                # renders one kind of inline decision and the answer never
                # has to be parsed out of what the customer typed.
                pause_block = {
                    "code": "write_confirmation_required",
                    "run_id": run_state.run_id,
                    "question": question,
                    "confirm_label": "Approve",
                    "reject_label": "Don't do it",
                    # Each queued write travels with its own interrupt id, so
                    # the console can render one control per write and send
                    # back a decision per id rather than a single yes covering
                    # a batch the customer only half agreed to.
                    "awaiting": [
                        {
                            "id": item["id"],
                            "tool": item["tool"],
                            "tool_use_id": item.get("tool_use_id", ""),
                            "args": item["args"],
                            "question": item["question"],
                            **(item.get("card") or {}),
                        }
                        for item in pending_interrupts
                    ],
                    "options": ["proceed", "reject"],
                }
                yield loop_event(
                    "human_approval",
                    run_state,
                    "Write paused at the harness until the customer confirms",
                    decision="ASK",
                    question=question,
                    awaiting=[
                        {"tool": item["tool"], "args": item["args"]}
                        for item in pending_interrupts
                    ],
                    enforced_by="HumanConfirmationHook",
                )
                yield loop_event(
                    "loop_decision",
                    run_state,
                    "Ask the customer before the write runs",
                    decision="ASK",
                )
                # No text_delta and no `final_reply`: the confirmation is a
                # harness control, not a reply. The model never wrote this
                # question, the conversation never receives it, and the turn
                # continues in place once the customer answers — so putting it
                # in the chat as an assistant message would stage a
                # conversation that did not happen.
                yield loop_event(
                    "completion",
                    run_state,
                    "Turn ended holding an unexecuted write",
                    exit_reason=run_state.exit_reason,
                    state=run_state.dump(),
                )
                yield {
                    "event": "done",
                    "data": json.dumps(
                        {
                            "final_reply": "",
                            "usage": usage_payload(run_state),
                            "pause": pause_block,
                        }
                    ),
                }
                return

            if run_state.budget_wrapup:
                # The guard stopped the loop one model call short of the
                # ceiling. Strands left its cancellation marker in the
                # conversation: drop it, so session memory ends exactly
                # where the loop stopped — on the observation the withheld
                # model call was about to read. Nothing else is added to it,
                # including the wrap-up below, which is written for the
                # customer and thrown away once answered. Continuing is then
                # just the next model call over unchanged context.
                _drop_marker_message(agent)
                run_state.status = "waiting"
                run_state.next_loop_state = "ASK"
                run_state.exit_reason = "PAUSED_AWAITING_USER_CONTINUE"
                run_state.pending_request = task_request
                run_state.resume_condition = (
                    f"customer approves another {run_state.token_budget:,}-token grant"
                )
                # The evaluation scores completion claims. This reply makes
                # none: it reports a partial state and asks a question, so
                # there is nothing to score.
                run_state.evaluation_status = "skipped"
                yield loop_event(
                    "loop_decision",
                    run_state,
                    "Stop the loop at the token guard and spend the reserved "
                    "call on a status report",
                    decision="ASK",
                    total_tokens_used=loop_tokens_used(run_state),
                    total_token_budget=run_state.token_budget,
                    input_tokens_used=run_state.input_tokens,
                    projected_next_call_input_tokens=run_state.projected_next_call_tokens,
                    resume_condition=run_state.resume_condition,
                )
                try:
                    wrapup, wrapup_usage = await wrapup_reply(
                        model=req.model or PROFILE.model,
                        goal=task_request.strip(),
                        session_messages=getattr(agent, "messages", []) or [],
                    )
                    add_auxiliary(run_state, wrapup_usage)
                except Exception:  # noqa: BLE001
                    log.exception("budget wrap-up call failed")
                    wrapup = ""
                if not wrapup:
                    # A paused turn still has to end with something the
                    # customer can answer, even if the wrap-up call failed.
                    wrapup = (
                        f"I paused partway through this request after "
                        f"{run_state.tool_call_count} checks and haven't changed "
                        "anything on your account. Shall I continue the rest of "
                        "the work, or stop here?"
                    )
                _AWAITING_CONTINUE[req.customer_id] = {
                    "run_id": run_state.run_id,
                    "goal": run_state.goal,
                    "pending_request": task_request,
                }
                # The console renders its own inline yes/no from this, so
                # the continue decision never depends on parsing the reply.
                pause_block = {
                    "code": "budget_grant_required",
                    "run_id": run_state.run_id,
                    "question": (
                        f"Add {run_state.token_budget:,} more tokens and "
                        "continue this task?"
                    ),
                    "confirm_label": "Continue",
                    "reject_label": "Stop",
                    "grant_tokens": run_state.token_budget,
                    "tokens_used": loop_tokens_used(run_state),
                    "token_ceiling": token_ceiling(run_state),
                    "grants_so_far": run_state.budget_grants,
                    "options": ["continue", "stop"],
                }
                yield loop_event(
                    "human_approval",
                    run_state,
                    "Budget guard hands the continue-or-stop decision to the customer",
                    decision="ASK",
                    question=pause_block["question"],
                    tokens_used=loop_tokens_used(run_state),
                    token_ceiling=pause_block["token_ceiling"],
                    grant_tokens=pause_block["grant_tokens"],
                    tool_calls_completed=run_state.tool_call_count,
                    enforced_by="TokenBudgetHook",
                )
                yield {"event": "text_delta", "data": json.dumps({"delta": wrapup})}
                yield loop_event(
                    "completion",
                    run_state,
                    "Turn ended at the budget guard with the work paused, not failed",
                    exit_reason=run_state.exit_reason,
                    state=run_state.dump(),
                )
                yield {
                    "event": "done",
                    "data": json.dumps(
                        {
                            "final_reply": wrapup,
                            "usage": usage_payload(run_state),
                            "pause": pause_block,
                        }
                    ),
                }
                return

            run_state.status = "complete"
            run_state.next_loop_state = "COMPLETE"
            run_state.exit_reason = "COMPLETE"
            run_state.resume_condition = None
            run_state.pending_request = None
            run_state.blocker = None
            yield loop_event(
                "loop_decision",
                run_state,
                "Release the reply",
                decision="COMPLETE",
            )
            yield {"event": "text_delta", "data": json.dumps({"delta": proposed_reply})}
            yield loop_event(
                "completion",
                run_state,
                "Reply released to the customer",
                exit_reason=run_state.exit_reason,
                state=run_state.dump(),
            )
            # The customer's answer is finished HERE. `done` closes the turn
            # in the console: the reply renders, the usage lands, and the
            # input unlocks. Nothing below can change any of that.
            yield {
                "event": "done",
                "data": json.dumps(
                    {"final_reply": proposed_reply, "usage": usage_payload(run_state)}
                ),
            }

            if not req.evaluation_enabled:
                run_state.evaluation_status = "skipped"
                return

            # --- Post-turn evaluation -----------------------------------
            #
            # Enforcement happened BEFORE the actions, at the tool boundary,
            # where CustomerIdBindingHook, RefundCapHook, RefundEvidenceHook
            # and the MCP server could still refuse. Everything here is an
            # opinion about work that is already done, so it runs AFTER the
            # reply rather than in front of it.
            #
            # The stream deliberately stays open past `done`. The console
            # reads to end-of-body and has already marked the turn complete,
            # so these frames attach to a turn the customer can read while
            # they arrive, and they never delay it.
            evaluation_results: list[dict[str, Any]] = []
            declared_contract = {
                "source": run_state.contract_source,
                "loaded_skills": run_state.loaded_skills,
                "required_criteria": run_state.success_criteria,
                "ordering": run_state.ordering_constraints,
                "action_preconditions": run_state.action_preconditions,
            }
            yield loop_event(
                "evaluation_started",
                run_state,
                "Reviewing the completed turn",
                aspects=[
                    {"aspect": key, "label": label}
                    for key, (label, _prompt, _slices) in EVALUATION_ASPECTS.items()
                ],
            )
            try:
                async for result, usage in evaluate_turn(
                    model=req.model or PROFILE.model,
                    customer_request=task_request,
                    proposed_reply=proposed_reply,
                    tool_history=run_state.tool_history,
                    tool_specs=tools,
                    declared_contract=declared_contract,
                    refund_cap_usd=effective_profile.refund_cap_usd,
                ):
                    # Review cost is real spend, so it is reported, but it is
                    # auxiliary: the budget guard does not meter it. Reviewing
                    # a turn never shortens the next one.
                    add_auxiliary(run_state, usage)
                    evaluation_results.append(result)
                    yield loop_event(
                        "evaluation_aspect",
                        run_state,
                        f"{result['label']}: {result['verdict']}",
                        evaluation=result,
                        model=req.model or PROFILE.model,
                    )
            except Exception:  # noqa: BLE001
                # The reply is already with the customer, so a broken review
                # pass degrades the console and nothing else.
                log.exception("post-turn evaluation failed")

            verdict = aggregate_verdict(evaluation_results)
            run_state.evaluation_status = verdict
            yield loop_event(
                "evaluation_complete",
                run_state,
                f"Review complete: {verdict}",
                verdict=verdict,
                aspects=evaluation_results,
                usage=usage_payload(run_state),
            )
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("agent stream failed")
            yield {"event": "error", "data": json.dumps({"message": str(exc)})}

    return EventSourceResponse(generator())


@app.get("/api/session")
def session(customer_id: str) -> dict[str, Any]:
    """The conversation this customer's agent currently remembers.

    Engineered keeps one agent per customer, so the session is scoped by
    `customer_id`. An agent that was never built for this customer has no
    memory yet — report an empty thread rather than building one.
    """
    from session_view import turns_from_messages

    agent = _AGENTS.get(customer_id)
    if agent is None:
        return {"turns": []}
    return {"turns": turns_from_messages(getattr(agent, "messages", []))}


@app.post("/api/reset")
def reset() -> dict[str, Any]:
    """Reset writable state: wipe engineered's mock data on disk and reseed,
    rebuild agents (which respawns MCP subprocesses that re-read the
    fresh data files), clear non-seed episodic memory."""
    # Mock data is file-backed on disk and scoped per-agent — wiping
    # engineered's files here leaves first-cut's state alone. The freshly spawned engineered
    # MCP subprocesses on the next /api/run pull from seeds.
    from mocks.client import reset_data_files
    reset_data_files(PROFILE.agent_id)

    # Dropping cached agents triggers a fresh build (and fresh MCP subprocesses
    # that read the now-reseeded data files) on the next request.
    _AGENTS.clear()
    _PENDING_CONFIRMATIONS.clear()
    _AWAITING_CONTINUE.clear()
    _RUNS.clear()

    # Episodic memory: drop everything EXCEPT the committed seed (cust_002).
    if EPISODIC_DIR.exists():
        for f in EPISODIC_DIR.glob("customer_*.md"):
            if f.name != "customer_cust_002.md":
                f.unlink()

    return {"ok": True, "agent": "cs_agent_engineered"}


@app.get("/api/state/{run_id}")
def get_run_state(run_id: str) -> dict[str, Any]:
    run_state = _RUNS.get(run_id)
    return {"exists": run_state is not None, "state": run_state.dump() if run_state else None}


@app.get("/api/ledger")
def ledger() -> dict[str, Any]:
    from mocks.client import CustomerSupportClient

    return {"entries": CustomerSupportClient(agent_id=PROFILE.agent_id).ledger_entries()}


class EndSessionRequest(BaseModel):
    customer_id: str | None = None


@app.post("/api/end_session")
def end_session(req: EndSessionRequest) -> dict[str, Any]:
    """Simulate 'time has passed' for the §5 episodic-memory demo. Drops the
    cached Agent for the given customer so the next request rebuilds it
    fresh — which wipes `agent.messages` (conversation memory) but RELOADS
    the customer's episodic memory file into the first user message. Mock
    backend state (orders, refunds, tickets) is untouched."""
    if req.customer_id:
        _AGENTS.pop(req.customer_id, None)
        _PENDING_CONFIRMATIONS.pop(req.customer_id, None)
        _AWAITING_CONTINUE.pop(req.customer_id, None)
    else:
        _AGENTS.clear()
        _PENDING_CONFIRMATIONS.clear()
        _AWAITING_CONTINUE.clear()
    return {"ok": True, "agent": "cs_agent_engineered", "ended": req.customer_id or "all"}
