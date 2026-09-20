"""The agent factory — `build_agent()`.

A thin loader. The agent has NO tools of its own — every tool comes from
an MCP server declared in `agent-profile.yaml`'s `mcp_servers` list, and
every `MCPClient` is passed straight to `Agent(tools=[...])` so Strands'
ToolProvider lifecycle owns the subprocesses end-to-end (`start()` on
first tool load; `stop()` when the Agent is GC'd / loses its last consumer).

For the §2a hands-on: edit the YAML to swap `customer_support_v1` for
`customer_support_v2`, type `exit`, run `python run.py` again. No
hot-reload — keeping the build path one direction makes the architecture
obvious on stage.
"""

import os
import sys
from pathlib import Path

from mcp import StdioServerParameters, stdio_client
from strands import Agent, AgentSkills
from strands.agent.conversation_manager import SlidingWindowConversationManager
from strands.models.openai import OpenAIModel
from context_trace import ContextTraceHook
from run_control import ToolBudgetHook
from strands.tools.mcp import MCPClient

from agent import memory
from agent.hooks import CustomerIdBindingHook, RefundCapHook
from agent.profile import MCPServerConfig, Profile, load_profile

# This service's root — cs_agent_v2/. Everything lives below it: skills,
# policies, memory, mocks, mcp_servers, profile YAML.
ROOT = Path(__file__).parent.parent
# Episodic memory directory — written by the `append_memory` / `compact_memory`
# tools, loaded into the system prompt at agent build. Conversation memory
# is now just `agent.messages` on the per-customer cached agent (see
# main.py's `_AGENTS`); no separate session-storage tree needed.


# ---------------------------------------------------------------------------
# MCP server construction
# ---------------------------------------------------------------------------


def make_mcp_client(config: MCPServerConfig) -> MCPClient:
    """Build a stdio MCPClient from a profile entry. ToolProvider — pass to `Agent(tools=[...])`."""
    full_env = dict(os.environ)
    full_env.update(config.env)
    # Resolve `python` / `python3` to the parent process's interpreter so
    # the MCP subprocess inherits the same venv (and therefore the same
    # mcp / pyyaml / strands packages). Without this the subprocess uses
    # whatever `python` happens to be on PATH — which on macOS is often
    # nothing, on a system Python it lacks our deps. Other commands pass
    # through unchanged.
    command = sys.executable if config.command in ("python", "python3") else config.command
    return MCPClient(
        lambda: stdio_client(
            StdioServerParameters(
                command=command,
                args=list(config.args),
                env=full_env,
            )
        )
    )


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


def _memory_protocol() -> str:
    return """You have episodic memory: short notes about prior sessions with each "
        "customer. When present for the current customer, the notes arrive at "
        "the top of their first user message of a new session, wrapped in "
        "`<episodic_memory>…</episodic_memory>` tags.\n\n"
        "How to use it:\n"
        "- **Pointers, not data.** Use notes to know what to look up and how to "
        "frame the reply. Numbers / IDs (refund refs, ticket IDs, amounts) MUST "
        "be re-verified via the matching read tool before you act on them — the "
        "audit ledger is the source of truth, memory may be stale.\n"
        "- **Tone matters.** A note like \"second damaged delivery; tone pointed\" "
        "should shape your phrasing and your escalation threshold.\n"
        "- **Close the loop.** Before the turn ends, call `append_memory(customer_id="
        "\"\", note=<short>)` with the things tools CAN'T tell the next session: "
        "open promises, patterns, tone. Strip anything an API would return. "
        "Keep it small."""


def prepend_memory(
    customer_id: str | None,
    prompt: str,
    compact_threshold: int = 0,
) -> str:
    """Wrap the customer's episodic memory file as XML and prepend it to the
    user message. Called once per Agent lifetime (first turn after build —
    so on every /api/run that arrives at a freshly-spawned cached Agent,
    typically right after /api/end_session). Returns the prompt unchanged
    if the customer has no memory file or none is configured."""
    if not customer_id:
        return prompt
    body = memory.load(customer_id)
    if not body.strip():
        return prompt
    notice = ""
    size = len(body)
    if compact_threshold and size > compact_threshold:
        notice = (
            f"\n\n[note to agent: memory above is {size:,} chars "
            f"(threshold {compact_threshold:,}). Compact via "
            f"`compact_memory(customer_id=\"\", new_content=<tighter rewrite>)` "
            f"before the turn ends.]"
        )
    return (
        f"<episodic_memory customer_id=\"{customer_id}\">\n"
        f"{body.strip()}{notice}\n"
        f"</episodic_memory>\n\n"
        f"{prompt}"
    )


def _build_instructions(
    profile: Profile,
    memory_section: str = "",
    skills_section: str = "",
) -> str:
    """Format `profile.system_prompt`; append any provided sections in order.

    The caller decides whether each section is included — `build_agent` builds
    the section string alongside the matching plugin/tool decision so the
    enable condition lives in one place per feature.
    """
    base = profile.system_prompt.format(**vars(profile))
    if memory_section:
        base += "\n\n" + memory_section
    if skills_section:
        base += "\n\n" + skills_section
    return base


# ---------------------------------------------------------------------------
# Build the agent
# ---------------------------------------------------------------------------


def frame_prompt(customer_id: str, prompt: str) -> str:
    """No-op for v2 — customer_id is bound by `CustomerIdBindingHook`, not inlined.

    Parallels `cs_agent_v1/agent.py::frame_prompt` (which DOES inline it).
    """
    _ = customer_id  # hook does the binding
    return prompt


def _resolve_skills_dir(profile: Profile) -> Path | None:
    """Return profile.skills_dir as an absolute Path, or None if unset / missing."""
    if not profile.skills_dir:
        return None
    path = Path(profile.skills_dir)
    if not path.is_absolute():
        path = ROOT / path
    return path if path.exists() else None


def build_agent(
    profile: Profile | None = None,
    customer_id: str | None = None,
) -> Agent:
    """Build the agent for one customer. `customer_id` is needed for episodic
    memory and hook binding. UI toggle overrides are baked into `profile`
    upstream via `apply_overrides` — this function just reads the profile."""
    if profile is None:
        profile = load_profile()

    # --- Tools: every MCP server in the YAML, passed as ToolProviders.
    # Strands handles subprocess lifecycle — see make_mcp_client docstring.
    mcp_clients = [make_mcp_client(cfg) for cfg in profile.mcp_servers]

    # --- Episodic memory: when enabled, attach the in-process write tools AND
    # include the protocol in the system prompt. Both together or neither —
    # the tool catalog must match what the prompt describes. The memory BODY
    # itself is injected into the first user message by main.py via
    # `prepend_memory(...)` once the cached Agent is fresh.
    local_tools: list = []
    memory_section = ""
    if profile.memory.episodic.enabled:
        memory_section = "## Episodic memory\n\n" + _memory_protocol()
        local_tools.extend([memory.append_memory, memory.compact_memory])

    # --- Skills: AgentSkills plugin auto-discovers SKILL.md under skills_dir,
    # injects the catalog into the system prompt under "## Available skills",
    # and registers a `skills` loader tool. When disabled (skills_dir is None),
    # none of that happens.
    plugins: list = []
    skills_section = ""
    skills_dir = _resolve_skills_dir(profile)
    if skills_dir:
        skills_section = "## Available skills\n\n"
        plugins.append(AgentSkills(skills=[str(skills_dir)]))

    # --- Session memory: `agent.messages` on the returned Agent, capped by
    # Strands' SlidingWindowConversationManager. main.py caches one Agent
    # per customer_id, so messages survive between requests (until window
    # overflow or /api/reset / process restart). Window size comes from
    # agent-profile.yaml's `memory.session.window`.
    conversation_manager = SlidingWindowConversationManager(
        window_size=profile.memory.session.window,
    )

    # --- User identity binding: the harness, not the LLM, is the principal.
    # Every customer-scoped tool call has its `customer_id` arg overwritten
    # with the session's trusted ID. Prompt injection can no longer cross
    # customer boundaries (OWASP API#1). See agent/hooks.py.
    hooks_: list = [ContextTraceHook(), ToolBudgetHook(mode="graceful")]
    if customer_id:
        hooks_.append(CustomerIdBindingHook(customer_id=customer_id))
    # Enforce the agent's scoped refund authority at the harness, so even a
    # prompt-injected agent cannot exceed its cap. The v2 MCP server has a
    # matching server-side check as defense in depth — the hook just makes
    # sure the bad call never reaches the wire.
    hooks_.append(
        RefundCapHook(
            refund_cap_usd=profile.refund_cap_usd,
            agent_id=profile.agent_id,
        )
    )

    agent = Agent(
        agent_id=profile.agent_id,
        name=profile.name,
        description=f"Customer support agent. Refund authority up to ${profile.refund_cap_usd:.2f}.",
        model=OpenAIModel(model_id=profile.model),
        system_prompt=_build_instructions(profile, memory_section, skills_section),
        tools=[*mcp_clients, *local_tools],
        plugins=plugins,
        hooks=hooks_,
        conversation_manager=conversation_manager,
        # Suppress Strands' default PrintingCallbackHandler — it streams text
        # chunks straight to stdout (no newlines), which collides with our own
        # trace rendering in run.py. We collect tokens from stream_async events
        # ourselves and render the final reply inside a Rich Panel.
        callback_handler=None,
    )
    return agent
