"""FastAPI entrypoint for cs_agent_first_cut — the first-cut, unscoped agent.

Run:  uvicorn main:app --reload --port 8001

Endpoints:
    POST /api/run    SSE stream of agent events for one prompt.
    POST /api/reset  Wipe this agent's mock data.
    GET  /health     Liveness probe.

A single Agent instance is built once at startup and reused across every
request — `agent.messages` accumulates in-process, so the agent has
short-term memory between turns. The catch: there's no per-customer
isolation, so the same conversation history is visible to every caller
(Alice's chat shows up in Bob's session). Process restart wipes it.
That's the first-cut footgun the lab demonstrates — engineered fixes it with a
per-customer agent cache.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

# The shared mocks/ package lives at the lab root, next to cs_agent_first_cut/
# and cs_agent_engineered/. Put it on sys.path before anything that imports it.
_LAB_ROOT = Path(__file__).parent.parent
if str(_LAB_ROOT) not in sys.path:
    sys.path.insert(0, str(_LAB_ROOT))

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

# OPENAI_API_KEY (and any other agent config) lives in a single `.env` at
# the repo root (`.env`) — one file to edit, both services read it.
# The per-agent `.env` is supported as an optional override.
load_dotenv(_LAB_ROOT / ".env")
load_dotenv(Path(__file__).parent / ".env", override=False)

from agent import AGENT_ID, build_agent, frame_prompt
from config import MODEL_ID, REFUND_CAP_USD
from planner import format_tool_specs, plan_for_prompt
from policy_evaluator import (
    ASPECTS as EVALUATION_ASPECTS,
    aggregate_verdict,
    evaluate_turn,
)
from mocks.client import arm_fault, disarm_fault
from loop_state import (
    RunStore,
    add_auxiliary,
    context_snapshot,
    event as loop_event,
    loop_tokens_used,
    record_iteration,
    record_observation,
    usage_payload,
)
from strands import Agent
from strands.models.openai import OpenAIModel
from tools import reset_state

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("cs_agent_first_cut")


# Single shared agent across all requests (and all customers). `agent.messages`
# is the conversation memory — survives across requests but leaks across
# users. Rebuilt on /api/reset (and lazily on first request).
_FIRST_CUT_AGENT: Agent | None = None
_RUNS = RunStore()


def _get_first_cut_agent() -> Agent:
    global _FIRST_CUT_AGENT
    if _FIRST_CUT_AGENT is None:
        _FIRST_CUT_AGENT = build_agent()
    return _FIRST_CUT_AGENT


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
    if name == "search_kb":
        return f'"{_truncate(args.get("query", ""), 50)}"'
    if name == "get_order":
        return args.get("order_id", "?")
    if name in {
        "get_customer_name",
        "get_customer_email",
        "get_customer_tier",
        "get_customer_verified",
        "get_customer_orders",
        "get_refund_history",
        "get_open_tickets",
    }:
        return args.get("customer_id", "?")
    if name == "modify_order":
        order = args.get("order_id", "?")
        # flat optional params — only show the ones the agent actually set
        actionable = {
            k: v
            for k, v in args.items()
            if k != "order_id" and v is not None
        }
        summary = ", ".join(
            f"{k}={_truncate(v, 25)}" for k, v in actionable.items()
        )
        return f"{order}, {summary}" if summary else order
    if name == "escalate":
        return f'"{_truncate(args.get("reason", ""), 50)}"'
    return ", ".join(f"{k}={_truncate(v, 25)}" for k, v in args.items())


_STR_ERROR_SENTINELS = (
    "error executing tool",
    "timed out",
    "not found",
    "couldn't",
    "couldnt",
    "unknown",
    "missing",
    "nope",
    "no results",
    "specify",
)


def _summarize_result(name: str, result: Any) -> tuple[str, bool]:
    # AP4: every tool signals errors differently. Cover each shape.
    if isinstance(result, dict):
        # modify_order: {"status": "ok"|"failed", ...}. Scoped to modify_order
        # because get_order also has a `status` key (the order's shipping
        # status, e.g. "in_transit_delayed") — without scoping, every order
        # lookup would render as a tool error.
        if name == "modify_order" and "status" in result:
            if result["status"] == "ok":
                return f"ok {result.get('ref', '')}", False
            return f"failed: {_truncate(result.get('message', '?'), 60)}", True
        # get_order: {"error": "..."}
        if "error" in result:
            return f"err: {_truncate(result['error'], 60)}", True

    if name == "get_order" and isinstance(result, dict):
        status = result.get("status", "?")
        total = result.get("total_usd", "?")
        late = result.get("delivery_days_late", 0)
        late_str = f", {late}d late" if late else ""
        return f"{status}, ${total}{late_str}", False

    # AP3 SOAP-style envelopes — peek into the nested structure to show
    # a useful one-liner instead of dumping the noise.
    if name == "get_refund_history" and isinstance(result, dict):
        env = result.get("RefundEnvelope") or {}
        return f"[{env.get('@count', '?')} refunds]", False
    if name == "get_open_tickets" and isinstance(result, dict):
        tlist = (result.get("TicketEnvelope") or {}).get("OpenTicketList") or {}
        return f"[{tlist.get('@itemCount', '?')} tickets]", False

    if isinstance(result, list):
        return f"[{len(result)} items]", False

    if isinstance(result, str):
        lowered = result.lower()
        is_err = any(sig in lowered for sig in _STR_ERROR_SENTINELS)
        return _truncate(result, 70), is_err

    if isinstance(result, bool):
        return "yes" if result else "no", False

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


async def _run_agent_stream(agent: Agent, prompt: str, *, run_state):
    """Yield SSE events for one agent turn."""
    # OpenAI reports the call's total input but not its internal split. The
    # budget hook uses this small estimate only once, to subtract the first
    # user message from the first exact input total and establish the stable
    # system-prompt + tool-contract baseline used by the context chart.
    agent._current_user_message_token_estimate = max(1, (len(prompt) + 3) // 4)
    yield {
        "event": "system_prompt",
        "data": json.dumps({"content": agent.system_prompt or ""}),
    }
    # Echo back the exact message we're about to hand the LLM — including
    # the framed session note. The audience needs to see what the agent
    # actually received, not just what the user typed.
    yield {
        "event": "user_message",
        "data": json.dumps({"content": prompt}),
    }

    pending: dict[str, dict] = {}
    agent._context_trace_snapshots = []
    emitted_contexts = 0

    async for event in agent.stream_async(prompt):
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
        if not isinstance(event, dict):
            continue

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
                slot = pending.get(tu_id)
                if not slot:
                    continue
                body = _extract_tool_result_body(block)
                summary, is_err = _summarize_result(slot["name"], body)
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
    customer_id: str  # Accepted for parity with engineered's API; first-cut does not bind
    #                 — the LLM picks whatever ID the user mentions in the
    #                 prompt. customer_id here is unused server-side, so a
    #                 prompt-injection attack on tenancy actually works.
    model: str | None = None  # Per-request override; falls back to MODEL_ID.
    # Per-request planner toggle. first-cut has no skills loader, so the planner
    # always runs with `skills_enabled=False`. Per-request, no rebuild
    # needed — the planner is a separate LLM call, not part of agent build.
    planner_enabled: bool | None = None
    run_id: str | None = None
    token_budget: int | None = None
    # One-shot demo fault: the next refund service call times out pre-commit.
    refund_service_timeout: bool = False


app = FastAPI(title="cs_agent_first_cut", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    # Vite dev server walks up the port range when its default is taken
    # (5173 → 5174 → 5175). Allow the typical span so the lab doesn't
    # silently fail with CORS errors when another dev server is up.
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1):51(7[0-9]|8[0-9])",
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "agent": "cs_agent_first_cut", "agent_id": AGENT_ID}


@app.get("/api/tools")
def tools_catalog() -> dict[str, Any]:
    """List the tools this agent has, with descriptions and input schemas.
    Tools don't change between requests, so the UI fetches this once per
    panel and renders a drawer next to the conversation."""
    agent = _get_first_cut_agent()
    return {
        "tools": agent.tool_registry.get_all_tool_specs(),
        "system_prompt": agent.system_prompt or "",
    }


@app.post("/api/run")
async def run(req: RunRequest):
    # Shared agent across all requests — `agent.messages` is conversation
    # memory. Mutating `agent.model` applies the per-request model choice
    # without rebuilding (which would wipe the message list). The shared
    # instance is what makes memory leak across customers — the lab's point.
    agent = _get_first_cut_agent()
    # The fault is one-shot and disk-backed, so it outlives the turn that armed
    # it unless the refund tool consumes it. Disarm whenever the toggle is off
    # so a primed fault can never surface on a later turn the console shows as
    # fault-free.
    if req.refund_service_timeout:
        arm_fault(AGENT_ID, "refund_service_timeout")
    else:
        disarm_fault(AGENT_ID, "refund_service_timeout")
    run_state = _RUNS.start(
        run_id=req.run_id,
        customer_id=req.customer_id,
        goal=req.prompt.strip(),
        token_budget=req.token_budget,
    )
    agent._active_run_state = run_state
    agent.model = OpenAIModel(model_id=req.model or MODEL_ID)

    # The prompt-framing step is in agent.py so the audience can grep
    # one file to see how customer_id ends up in the LLM-visible message.
    framed_prompt = frame_prompt(req.customer_id, req.prompt)

    # Planning layer: same module as engineered (../planner.py at the lab root),
    # called the same way. first-cut has no skills loader, so we always pass
    # `skills_enabled=False` — the planner is told skills aren't an
    # option here and won't suggest any.
    plan = ""
    if req.planner_enabled:
        try:
            tools_catalogue = format_tool_specs(
                agent.tool_registry.get_all_tool_specs()
            )
            plan, planner_usage = await plan_for_prompt(
                req.prompt,
                model=req.model or MODEL_ID,
                tools_catalogue=tools_catalogue,
                skills_enabled=False,
            )
            add_auxiliary(run_state, planner_usage)
        except Exception:  # noqa: BLE001
            # Planner failures (timeout, rate limit) shouldn't take the
            # turn down — fall back to the unplanned prompt.
            log.exception("planner call failed; proceeding without a plan")
            plan = ""

    if plan:
        framed_prompt = f"{plan}\n\n{framed_prompt}"

    async def generator():
        proposed_reply = ""
        tools = agent.tool_registry.get_all_tool_specs()
        snapshot = context_snapshot(
            variant="first_cut",
            prompt=framed_prompt,
            system_prompt=agent.system_prompt or "",
            message_count=len(getattr(agent, "messages", []) or []),
            tool_names=_tool_names(tools),
            plan=plan,
        )
        yield loop_event(
            "context_build",
            run_state,
            "Accumulated conversation, broad prompt, and full tool surface",
            context=snapshot,
            provenance={
                "goal": "customer request",
                "success_criteria": "none; model stop is accepted",
                "scenario_metadata_received": False,
                "run_state_in_model_context": False,
            },
        )
        if plan:
            yield {"event": "plan", "data": json.dumps({"content": plan})}
        try:
            async for ev in _run_agent_stream(agent, framed_prompt, run_state=run_state):
                if ev.get("event") == "tool_call":
                    body = json.loads(ev.get("data", "{}"))
                    yield loop_event(
                        "llm_decision",
                        run_state,
                        f"Invoke {body.get('name', 'tool')}",
                        selected_action=body.get("name"),
                    )
                yield ev
                if ev.get("event") == "tool_result":
                    body = json.loads(ev.get("data", "{}"))
                    record_observation(
                        run_state,
                        tool_name=body.get("name", "tool"),
                        observation=body.get("result"),
                        is_error=bool(body.get("is_error")),
                        tool_use_id=body.get("tool_use_id") or None,
                    )
                    if "timed out" in json.dumps(body.get("result"), default=str).lower():
                        yield loop_event(
                            "recovery",
                            run_state,
                            "Refund service timed out before completing the write",
                            fault="refund_service_timeout",
                            decision="model_must_choose_recovery",
                        )
                    yield loop_event(
                        "state_transition",
                        run_state,
                        "Recorded the latest tool observation",
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
                        "Continue after observation",
                        decision="CONTINUE",
                    )
                if ev.get("event") == "done":
                    proposed_reply = json.loads(ev.get("data", "{}")).get(
                        "final_reply", ""
                    )
                    if run_state.next_loop_state == "BUDGET_EXHAUSTED":
                        yield loop_event(
                            "completion",
                            run_state,
                            "Token budget exceeded before the request completed",
                            exit_reason=run_state.exit_reason,
                            state=run_state.dump(),
                        )
                        yield {
                            "event": "error",
                            "data": json.dumps(
                                {
                                    "message": (
                                        f"Token budget exceeded at "
                                        f"{loop_tokens_used(run_state):,}/"
                                        f"{run_state.token_budget:,} total tokens "
                                        f"after {run_state.tool_call_count} tool calls."
                                    )
                                }
                            ),
                        }
                        return
                    run_state.status = "complete"
                    run_state.next_loop_state = "STOP"
                    run_state.exit_reason = "MODEL_STOP"
                    yield loop_event(
                        "completion",
                        run_state,
                        "Model stopped producing actions",
                        exit_reason="MODEL_STOP",
                        state=run_state.dump(),
                    )

            # The answer has already been released. Review the same evidence
            # bundle used for engineered so the comparison is symmetric.
            evaluation_results: list[dict[str, Any]] = []
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
                    model=req.model or MODEL_ID,
                    customer_request=req.prompt,
                    proposed_reply=proposed_reply,
                    tool_history=run_state.tool_history,
                    declared_contract=None,
                    refund_cap_usd=REFUND_CAP_USD,
                ):
                    add_auxiliary(run_state, usage)
                    evaluation_results.append(result)
                    yield loop_event(
                        "evaluation_aspect",
                        run_state,
                        f"{result['label']}: {result['verdict']}",
                        evaluation=result,
                        model=req.model or MODEL_ID,
                    )
            except Exception:  # noqa: BLE001
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
        except Exception as exc:  # noqa: BLE001
            log.exception("agent stream failed")
            yield {"event": "error", "data": json.dumps({"message": str(exc)})}

    return EventSourceResponse(generator())


@app.get("/api/session")
def session() -> dict[str, Any]:
    """The conversation the agent currently remembers.

    `agent.messages` is the session memory. The console keeps its own copy of
    the thread in React state, which a browser refresh throws away — this lets
    it rebuild the thread from what the agent actually holds instead of
    showing an empty panel beside a non-empty agent.
    """
    from session_view import turns_from_messages

    if _FIRST_CUT_AGENT is None:
        return {"turns": []}
    return {"turns": turns_from_messages(getattr(_FIRST_CUT_AGENT, "messages", []))}


@app.post("/api/reset")
def reset() -> dict[str, Any]:
    """Reset this agent's mock data and drop the shared agent instance so
    its `agent.messages` (conversation memory) is wiped on next request."""
    global _FIRST_CUT_AGENT
    _FIRST_CUT_AGENT = None
    reset_state()
    _RUNS.clear()
    return {"ok": True, "agent": "cs_agent_first_cut"}


@app.get("/api/state/{run_id}")
def get_run_state(run_id: str) -> dict[str, Any]:
    run_state = _RUNS.get(run_id)
    return {"exists": run_state is not None, "state": run_state.dump() if run_state else None}


@app.get("/api/ledger")
def ledger() -> dict[str, Any]:
    from mocks.client import CustomerSupportClient

    return {"entries": CustomerSupportClient(agent_id=AGENT_ID).ledger_entries()}


class EndSessionRequest(BaseModel):
    customer_id: str | None = None


@app.post("/api/end_session")
def end_session(req: EndSessionRequest) -> dict[str, Any]:
    """Simulate 'time has passed' for the §5 episodic-memory demo. first-cut has a
    single shared Agent across all callers (the leaky-memory footgun), so
    dropping it wipes conversation memory for EVERY customer in one shot —
    which is itself a hint that the "scope" of memory was wrong. first-cut also
    has no episodic memory layer, so the next request starts genuinely
    cold. Mock backend state is untouched. The `customer_id` field is
    accepted for parity with engineered's API but ignored — there's only one
    shared agent here, not a per-customer cache."""
    global _FIRST_CUT_AGENT
    _FIRST_CUT_AGENT = None
    _ = req.customer_id  # accepted for parity; first-cut's shared agent ignores it
    return {"ok": True, "agent": "cs_agent_first_cut", "ended": "all"}
