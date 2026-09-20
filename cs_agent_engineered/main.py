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
from agent.profile import apply_overrides, load_profile
from agent.skill_contracts import load_skill_contract
from planner import format_tool_specs, plan_for_prompt, skills_catalogue_from_dir
from loop_state import (
    RunStore,
    apply_task_contract,
    context_snapshot,
    event as loop_event,
    grace_threshold_calls,
    record_iteration,
    record_observation,
    validate_response,
    validation_feedback,
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
    if name == "get_policy":
        return args.get("policy_id", "?")
    if name == "list_policies":
        return ""
    if name == "issue_refund":
        pct = args.get("refund_percentage")
        pct_str = f"{pct:.0%}" if isinstance(pct, (int, float)) else "?"
        return (
            f"{args.get('order_id', '?')}, "
            f"{pct_str}, "
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
        return f"{status}, ${total}{late_str}", False
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


async def _run_agent_stream(agent: Agent, prompt: str, *, run_state):
    """Yield SSE events for one agent turn."""
    # Echo the exact message handed to the LLM. engineered doesn't frame anything
    # at the message layer (identity is bound by CustomerIdBindingHook at
    # the tool layer), so this is just the raw prompt.
    yield {
        "event": "user_message",
        "data": json.dumps({"content": prompt}),
    }

    # `system_prompt` is emitted AFTER the stream begins so the AgentSkills
    # plugin (which injects its skills catalog via a BeforeInvocationEvent
    # hook) has had a chance to update `agent.system_prompt` first. If we
    # emitted it before stream_async, the drawer in the UI would show the
    # bare prompt without the "Available skills" catalog.
    system_prompt_emitted = False
    agent._context_trace_snapshots = []
    emitted_contexts = 0

    pending: dict[str, dict] = {}  # tool_use_id -> {name, args, emitted_call}

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
        if not system_prompt_emitted:
            yield {
                "event": "system_prompt",
                "data": json.dumps({"content": agent.system_prompt or ""}),
            }
            system_prompt_emitted = True

        if not isinstance(event, dict):
            continue

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

    # Fallback: if the stream yielded zero events (rare — error before any
    # output), emit the (now-injected) system_prompt so the drawer renders.
    if not system_prompt_emitted:
        yield {
            "event": "system_prompt",
            "data": json.dumps({"content": agent.system_prompt or ""}),
        }

    snapshots = getattr(agent, "_context_trace_snapshots", [])
    while emitted_contexts < len(snapshots):
        snapshot = snapshots[emitted_contexts]
        emitted_contexts += 1
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

    yield {"event": "done", "data": json.dumps({"final_reply": final_reply})}


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------


class RunRequest(BaseModel):
    prompt: str
    customer_id: str
    model: str | None = None  # Per-request override; falls back to PROFILE.model.
    # UI toggles for engineered features. Null falls back to profile defaults.
    # `skills_enabled` and `episodic_enabled` require a session reset (the
    # agent is cached and its system_prompt / tools / plugins are baked at
    # build time), so the frontend calls /api/reset before sending a request
    # with new values. `planner_enabled` is a per-request decision (the
    # planner is a separate LLM call, not part of the agent build), so it
    # can flip freely without a reset.
    skills_enabled: bool | None = None
    episodic_enabled: bool | None = None
    planner_enabled: bool | None = None
    run_id: str | None = None
    tool_budget: int | None = None


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
        "system_prompt": agent.system_prompt or "",
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


@app.post("/api/run")
async def run(req: RunRequest):
    # Apply UI toggle overrides to a per-request profile copy. From here on
    # the profile IS the source of truth — no separate flag plumbing.
    effective_profile = apply_overrides(
        PROFILE,
        skills_enabled=req.skills_enabled,
        episodic_enabled=req.episodic_enabled,
        planner_enabled=req.planner_enabled,
    )
    agent = get_agent(req.customer_id, effective_profile)
    run_state = _RUNS.start(
        run_id=req.run_id,
        customer_id=req.customer_id,
        goal=req.prompt.strip(),
        tool_budget=req.tool_budget,
    )
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
            plan = await plan_for_prompt(
                req.prompt,
                model=planner_model,
                tools_catalogue=tools_catalogue,
                skills_catalogue=skills_cat,
                skills_enabled=bool(skills_dir),
            )
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
    if not agent.messages and effective_profile.memory.episodic.enabled:
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
            run_state.verified_criteria["memory_selected"] = True
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
        try:
            attempt_prompt = framed_prompt
            max_validation_attempts = 2
            skill_calls: dict[str, str] = {}
            for attempt in range(max_validation_attempts):
                proposed_reply = ""
                internal_retry = attempt > 0
                async for ev in _run_agent_stream(
                    agent, attempt_prompt, run_state=run_state
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
                            validation_attempt=attempt + 1,
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
                    # The proposed answer stays private until validation. The
                    # retry prompt is also internal control feedback, not a new
                    # customer message.
                    if event_type not in {"text_delta", "done"} and not (
                        internal_retry and event_type in {"user_message", "system_prompt"}
                    ):
                        yield ev
                    if event_type == "tool_result":
                        body = json.loads(ev.get("data", "{}"))
                        record_observation(
                            run_state,
                            tool_name=body.get("name", "tool"),
                            observation=body.get("result"),
                            is_error=bool(body.get("is_error")),
                        )
                        if body.get("name") == "skills":
                            skill_name = skill_calls.get(body.get("tool_use_id", ""), "")
                            contract = None
                            if skill_name and effective_profile.skills_dir:
                                skills_root = Path(effective_profile.skills_dir)
                                if not skills_root.is_absolute():
                                    skills_root = ROOT / skills_root
                                contract = load_skill_contract(skills_root, skill_name)
                            if not body.get("is_error"):
                                run_state.verified_criteria["procedure_loaded"] = True
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
                        if body.get("tool_use_id") and any(
                            marker in str(body.get("result", ""))
                            for marker in ("saved_for", "rewrote")
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

                if run_state.next_loop_state == "PAUSE":
                    run_state.pending_request = req.prompt
                    run_state.validation_attempts = attempt + 1
                    run_state.validation_status = "passed"
                    pause_reply = (
                        f"I completed {run_state.tool_call_count} tool calls and paused before "
                        "the safety limit. The completed observations remain in this run; "
                        "continue in a new turn with a fresh budget."
                    )
                    yield loop_event(
                        "validation",
                        run_state,
                        "Pause preserves completed evidence and a resume condition",
                        validation={
                            "passed": True,
                            "decision": "PAUSE",
                            "resume_condition": run_state.resume_condition,
                        },
                    )
                    yield {"event": "text_delta", "data": json.dumps({"delta": pause_reply})}
                    yield loop_event(
                        "completion",
                        run_state,
                        "Run paused before the next tool dispatch",
                        exit_reason=run_state.exit_reason,
                        state=run_state.dump(),
                    )
                    yield {"event": "done", "data": json.dumps({"final_reply": pause_reply})}
                    return

                run_state.validation_attempts = attempt + 1
                validation = validate_response(run_state, proposed_reply)
                yield loop_event(
                    "validation",
                    run_state,
                    "Proposed reply passed the release gate"
                    if validation["passed"]
                    else "Proposed reply failed the release gate",
                    validation=validation,
                    attempt=attempt + 1,
                )

                if validation["passed"]:
                    run_state.status = "complete"
                    run_state.next_loop_state = "COMPLETE"
                    run_state.exit_reason = "VALIDATED_COMPLETE"
                    yield loop_event(
                        "loop_decision",
                        run_state,
                        "Release the validated reply",
                        decision="COMPLETE",
                    )
                    yield {"event": "text_delta", "data": json.dumps({"delta": proposed_reply})}
                    yield loop_event(
                        "completion",
                        run_state,
                        "Validated reply released to the customer",
                        exit_reason=run_state.exit_reason,
                        state=run_state.dump(),
                    )
                    yield {"event": "done", "data": json.dumps({"final_reply": proposed_reply})}
                    return

                irreversible_violation = bool(validation["violations"])
                budget_allows_retry = run_state.tool_call_count < grace_threshold_calls(run_state)
                if attempt + 1 < max_validation_attempts and not irreversible_violation and budget_allows_retry:
                    feedback = validation_feedback(validation)
                    run_state.next_loop_state = "CONTINUE"
                    yield loop_event(
                        "loop_decision",
                        run_state,
                        "Validation found missing work; continue before replying",
                        decision="CONTINUE",
                        feedback=feedback,
                    )
                    attempt_prompt = (
                        "<validation_feedback>\n"
                        "Your proposed response has not been shown to the customer. "
                        "Complete the missing checks or actions, then produce a corrected final reply.\n"
                        f"Issues: {feedback}\n"
                        f"Original customer request: {req.prompt}\n"
                        "</validation_feedback>"
                    )
                    continue

                run_state.status = "stopped"
                run_state.next_loop_state = "ESCALATE" if irreversible_violation else "STOP"
                run_state.exit_reason = (
                    "VALIDATION_VIOLATION" if irreversible_violation else "VALIDATION_INCOMPLETE"
                )
                missing_text = ", ".join(validation["missing_criteria"])
                safe_reply = (
                    "I’m not ready to give you a definitive answer yet because the required checks "
                    f"are incomplete ({missing_text or 'a safety rule was violated'}). "
                    "I have not treated this request as complete."
                )
                yield loop_event(
                    "loop_decision",
                    run_state,
                    "Block the unvalidated reply",
                    decision=run_state.next_loop_state,
                    validation=validation,
                )
                yield {"event": "text_delta", "data": json.dumps({"delta": safe_reply})}
                yield loop_event(
                    "completion",
                    run_state,
                    "Unvalidated reply withheld",
                    exit_reason=run_state.exit_reason,
                    state=run_state.dump(),
                )
                yield {"event": "done", "data": json.dumps({"final_reply": safe_reply})}
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


@app.post("/api/evaluations/run")
def run_evaluations() -> dict[str, Any]:
    from evaluations import run_suite

    return run_suite(_LAB_ROOT)


class EndSessionRequest(BaseModel):
    customer_id: str | None = None


@app.post("/api/end_session")
def end_session(req: EndSessionRequest) -> dict[str, Any]:
    """Simulate 'time has passed' for the §5 episodic-memory demo. Drops the
    cached Agent for the given customer so the next request rebuilds it
    fresh — which wipes `agent.messages` (conversation memory) but RELOADS
    the customer's episodic memory file into the new system prompt. Mock
    backend state (orders, refunds, tickets) is untouched."""
    if req.customer_id:
        _AGENTS.pop(req.customer_id, None)
    else:
        _AGENTS.clear()
    return {"ok": True, "agent": "cs_agent_engineered", "ended": req.customer_id or "all"}
