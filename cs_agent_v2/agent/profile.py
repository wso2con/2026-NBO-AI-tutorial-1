"""Load the agent profile from agent-profile.yaml.

The YAML is the complete declarative description of the agent: identity,
model, tool set, skill directory, MCP servers, memory config, refund cap.
`agent/core.py` is a loader on top of this.
"""

from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

PROFILE_PATH = Path(__file__).parent.parent / "agent-profile.yaml"


@dataclass
class SessionMemory:
    # Within-session chat history. `agent.messages` capped by Strands'
    # SlidingWindowConversationManager; oldest messages dropped on overflow.
    window: int = 20


@dataclass
class EpisodicMemory:
    # Past-sessions per-customer summary in memory/customer_<id>.md, loaded
    # fully into the system prompt at agent build.
    enabled: bool = False
    # Char-count above which the system prompt nudges the agent to call
    # `compact_memory()` to summarize. 0 disables the nudge.
    compact_threshold: int = 4000


@dataclass
class MemoryConfig:
    session: SessionMemory = field(default_factory=SessionMemory)
    episodic: EpisodicMemory = field(default_factory=EpisodicMemory)


@dataclass
class PlannerConfig:
    # When enabled, every /api/run kicks off a small non-streaming LLM call
    # BEFORE the main agent to produce a <plan> block (intent, approach,
    # info_needed, skills, policies). The plan is prepended to the user
    # message. See `agent/planner.py`.
    enabled: bool = False


@dataclass
class MCPServerConfig:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


# Minimal fallback if agent.system_prompt is missing from YAML. Keep this
# functional but stark so the user notices and restores the real template.
_DEFAULT_SYSTEM_PROMPT = (
    "You are {name} (agent_id={agent_id}). Help customers with order issues. "
    "You can issue refunds up to ${refund_cap_usd:.2f}; escalate larger ones. "
    "(NOTE: `agent.system_prompt` is missing from agent-profile.yaml — "
    "restore the full template from the lab README.)"
)


@dataclass
class Profile:
    agent_id: str
    name: str
    model: str
    language: str
    system_prompt: str
    skills_dir: str | None
    mcp_servers: list[MCPServerConfig]
    memory: MemoryConfig
    planner: PlannerConfig
    refund_cap_usd: float


def apply_overrides(
    profile: Profile,
    skills_enabled: bool | None = None,
    episodic_enabled: bool | None = None,
    planner_enabled: bool | None = None,
) -> Profile:
    """Return a Profile copy with UI toggle overrides applied. `None` keeps
    the YAML default. Disabling skills wipes `skills_dir`; disabling episodic
    flips `memory.episodic.enabled`; toggling the planner flips
    `planner.enabled` (a per-request decision — no agent rebuild needed).
    The returned Profile is the single source of truth for `build_agent`
    and the per-request planner gate — no extra flag plumbing required."""
    if (
        skills_enabled is None
        and episodic_enabled is None
        and planner_enabled is None
    ):
        return profile

    new_skills_dir = profile.skills_dir if skills_enabled is not False else None
    new_episodic = profile.memory.episodic
    if episodic_enabled is not None:
        new_episodic = replace(profile.memory.episodic, enabled=episodic_enabled)
    new_planner = profile.planner
    if planner_enabled is not None:
        new_planner = replace(profile.planner, enabled=planner_enabled)

    return replace(
        profile,
        skills_dir=new_skills_dir,
        memory=replace(profile.memory, episodic=new_episodic),
        planner=new_planner,
    )


def load_profile(path: Path = PROFILE_PATH) -> Profile:
    """Read agent-profile.yaml and return a typed Profile."""
    if not path.exists():
        raise FileNotFoundError(f"agent-profile.yaml not found at {path}. See README.")
    raw = yaml.safe_load(path.read_text())

    agent_section = raw.get("agent", {})
    memory_section = raw.get("memory", {}) or {}
    session_section = memory_section.get("session", {}) or {}
    episodic_section = memory_section.get("episodic", {})
    # Tolerate the shorthand `episodic: true` / `episodic: false`.
    if isinstance(episodic_section, bool):
        episodic_section = {"enabled": episodic_section}
    elif episodic_section is None:
        episodic_section = {}
    planner_section = raw.get("planner", {})
    # Tolerate the shorthand `planner: true` / `planner: false`.
    if isinstance(planner_section, bool):
        planner_section = {"enabled": planner_section}
    elif planner_section is None:
        planner_section = {}
    mcp_section = raw.get("mcp_servers") or []

    mcp_servers = [
        MCPServerConfig(
            name=entry["name"],
            command=entry["command"],
            args=list(entry.get("args", [])),
            env=dict(entry.get("env", {})),
        )
        for entry in mcp_section
    ]

    return Profile(
        agent_id=agent_section.get("id", "cs-agent-v1"),
        name=agent_section.get("name", "Customer Support Agent"),
        model=agent_section.get("model", "gpt-4o-2024-08-06"),
        language=agent_section.get("language", "English"),
        system_prompt=agent_section.get("system_prompt") or _DEFAULT_SYSTEM_PROMPT,
        skills_dir=raw.get("skills_dir"),
        mcp_servers=mcp_servers,
        memory=MemoryConfig(
            session=SessionMemory(window=int(session_section.get("window", 20))),
            episodic=EpisodicMemory(
                enabled=bool(episodic_section.get("enabled", False)),
                compact_threshold=int(episodic_section.get("compact_threshold", 4000)),
            ),
        ),
        planner=PlannerConfig(
            enabled=bool(planner_section.get("enabled", False)),
        ),
        refund_cap_usd=float(raw.get("refund_cap_usd", 200.0)),
    )
