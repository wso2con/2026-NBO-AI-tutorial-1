"""Planner — a small LLM call that runs BEFORE the main agent on every turn.

Shared module: both `cs_agent_v1` and `cs_agent_v2` import this same file
from the lab root. Per-agent differences (which tools exist, whether
skills are wired) are passed in via keyword arguments — the planner
itself is agent-agnostic.

The planner has one job: read the customer's message and produce a short
<plan> block that scopes the agent's work. It does NOT call tools. The
plan is prepended to the user message so the main agent reads
"intent + approach + information needs + relevant skills + relevant
policies" before it picks its first tool.

Why a separate call:
- The same LLM that does intent recognition AND tool calling tends to drop
  the intent half once tools become available. A dedicated planner call
  has no tool affordances, so it cannot skip ahead.
- Plans are per-turn, not per-build. They live in the user-message space
  alongside the prompt they reasoned about.
- Same model family as the main agent (gpt-5.4-mini by default) keeps
  latency low — ~1s for a non-streaming completion at ~150 output tokens.

Catalogue strategy:
- **Tools**: passed in from the caller. main.py extracts them from the
  built agent's `tool_registry` so the planner sees exactly what the main
  agent has, with no drift. When called without a `tools_catalogue`
  override, falls back to a hardcoded list (kept for tests / REPL).
- **Skills**: caller passes either a catalogue string or sets
  `skills_enabled=False` to drop the skills section entirely. v1 has no
  skills loader, so it always passes `skills_enabled=False`. v2 passes
  `skills_enabled=bool(profile.skills_dir)`.
- **Policies**: auto-discovered from `policies/*.md` frontmatter at the
  lab root (same `policies/` directory both agents share).
"""

from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path

from openai import AsyncOpenAI


_LAB_ROOT = Path(__file__).parent
_POLICIES_DIR = _LAB_ROOT / "policies"


# ----- Catalogue loaders --------------------------------------------------


def _parse_frontmatter(content: str) -> dict[str, str]:
    """Tiny YAML-frontmatter reader. Strings + bare values only; skips
    nested structures. Returns {} if no `---` fence at the top."""
    if not content.startswith("---"):
        return {}
    end = content.find("\n---", 3)
    if end == -1:
        return {}
    out: dict[str, str] = {}
    for line in content[3:end].strip().split("\n"):
        m = re.match(r"([a-zA-Z_][\w-]*):\s*(.*)", line)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[key] = value
    return out


def skills_catalogue_from_dir(skills_dir: Path | str) -> str:
    """`name — description` per SKILL.md frontmatter, sorted by skill dir.

    Public helper: callers (i.e. v2's main.py) can pass their resolved
    skills directory in. Used by `plan_for_prompt`'s default skill-catalogue
    fallback when `skills_enabled=True` and no override is supplied.
    """
    path = Path(skills_dir)
    if not path.exists():
        return "(no skills configured)"
    lines: list[str] = []
    for skill_dir in sorted(path.iterdir()):
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        fm = _parse_frontmatter(skill_md.read_text())
        name = fm.get("name", skill_dir.name)
        desc = fm.get("description", "")
        lines.append(f"- {name} — {desc}" if desc else f"- {name}")
    return "\n".join(lines) or "(no skills)"


def _policies_catalogue() -> str:
    """`id — title` per policy file frontmatter (title acts as description)."""
    if not _POLICIES_DIR.exists():
        return "(no policies configured)"
    lines: list[str] = []
    for path in sorted(_POLICIES_DIR.glob("*.md")):
        fm = _parse_frontmatter(path.read_text())
        pid = fm.get("id", path.stem)
        title = fm.get("title", "")
        lines.append(f"- {pid} — {title}" if title else f"- {pid}")
    return "\n".join(lines) or "(no policies)"


def format_tool_specs(tool_specs: list[dict]) -> str:
    """Format a list of Strands tool specs (`agent.tool_registry.get_all_tool_specs()`)
    into the planner's `name — first line of description` form.

    Filters out the `skills` meta-tool because individual skills get their
    own section in the planner prompt — including it under tools too would
    just be noise.
    """
    lines: list[str] = []
    for spec in tool_specs:
        inner = spec.get("toolSpec", spec)
        name = inner.get("name", "")
        if not name or name == "skills":
            continue
        desc = (inner.get("description") or "").strip()
        first_line = desc.splitlines()[0].strip() if desc else ""
        lines.append(f"- {name} — {first_line}" if first_line else f"- {name}")
    return "\n".join(lines) or "(no tools)"


def _tools_catalogue_fallback() -> str:
    """Hardcoded mirror of v2's MCP tool surface, used only when the caller
    doesn't pass a live tools catalogue (tests, REPL). v1's tools are
    differently shaped, so v1 must always pass a live catalogue via
    `format_tool_specs(agent.tool_registry.get_all_tool_specs())`."""
    return """\
- lookup_customer — customer profile (name, tier, contact)
- get_order — one order: status, items, total, address
- get_customer_orders — recent orders for a customer
- get_open_tickets — tickets currently open for a customer
- get_refund_history — all prior refunds, with refund_percentage per entry
- update_shipping_address — change address (only if not shipped)
- cancel_order — cancel (only if not shipped). Does NOT auto-refund.
- issue_refund — refund as a percentage of the order total
- escalate_to_human — open a human ticket
- search_policy_kb — search the policy knowledge base
- append_memory — one short note to this customer's episodic memory
- compact_memory — rewrite the episodic memory file"""


# ----- Planner prompt + call ----------------------------------------------


def _planner_system_prompt(
    tools_catalogue: str,
    skills_catalogue: str | None,
    policies_catalogue: str,
) -> str:
    """Build the planner's system prompt.

    `skills_catalogue=None` is the signal that skills are disabled for this
    turn (the main agent doesn't have the AgentSkills plugin loaded). In
    that case the skills section, the `skills:` output field, and the
    skills-related rule are all dropped — the planner can't suggest a
    skill the main agent has no way to load.
    """
    today = date.today().isoformat()

    skills_on = skills_catalogue is not None
    skills_output_field = (
        "skills:\n- <skills the main agent should load before acting; omit the section if none apply>\n"
        if skills_on
        else ""
    )
    skills_rule = (
        "- `skills` is a hint, not a mandate. List only the ones whose procedure clearly matches the intent. If no skill applies, omit the section."
        if skills_on
        else "- Skills are NOT available this turn. Do not include a `skills` field in your <plan>."
    )
    skills_section = (
        f"\nSkills the main agent can load (procedure documents):\n{skills_catalogue}\n"
        if skills_on
        else ""
    )

    return f"""You are the planning layer of a customer support agent. Your only job is to produce one short <plan> block that scopes the next turn for the main agent BEFORE it reaches for tools.

You do NOT call tools. You output exactly one <plan> block, then stop.

Output format (exact tags, nothing outside <plan>):

<plan>
intent: <one line. What the customer ACTUALLY wants — distinguish what they're asking ABOUT from what they NEED. The two are often different. "My order is late, I'm flying tomorrow" → they want the package in time, not money. "Cancel my order" with no urgency → they want out, not faster delivery.>
approach: <one or two lines. High-level direction. Order matters: try the customer-preferred outcome first, fallbacks after. Compensation is usually a fallback, not the goal.>
info_needed:
- <concrete items the agent should look up>
{skills_output_field}policies:
- <relevant policy IDs to consult>
</plan>

Rules:
- Be terse. The whole plan fits in 10–15 lines.
- Do NOT list step-by-step tool calls; the main agent picks tools. You scope the problem.
- Do NOT invent tools, skills, or policies. Only reference items from the lists below.
{skills_rule}
- For a purely informational turn, a one-line intent + one policy is enough.
- If you genuinely cannot infer intent from the message, say so in `intent:` — do not guess.

Tools the main agent has:
{tools_catalogue}
{skills_section}
Policies the main agent can search:
{policies_catalogue}

Today's date: {today}
"""


async def plan_for_prompt(
    prompt: str,
    model: str,
    *,
    tools_catalogue: str | None = None,
    skills_catalogue: str | None = None,
    policies_catalogue: str | None = None,
    skills_enabled: bool = True,
) -> str:
    """Generate a <plan> block for the customer's next-turn message.

    Args:
        prompt: The customer's raw message.
        model:  The OpenAI model id (same family as the main agent).
        tools_catalogue: Optional override for the tools section. When the
            caller has a built agent on hand, pass
            `format_tool_specs(agent.tool_registry.get_all_tool_specs())`
            so the planner sees the live tool surface. Defaults to a
            hardcoded fallback.
        skills_catalogue: Optional override for the skills section. When
            `skills_enabled=True` and this is None, the section reads
            "(no skills catalogue provided)" — callers that have skills
            wired should pass `skills_catalogue_from_dir(skills_dir)`.
        policies_catalogue: Optional override for the policies section.
            Defaults to disk-based discovery of `policies/*.md` frontmatter
            at the lab root.
        skills_enabled: When False, the planner is told skills aren't
            available this turn and is instructed NOT to emit a `skills:`
            field. Pass `False` whenever the main agent doesn't have the
            AgentSkills plugin loaded (v1 always; v2 when the skills
            toggle is off). Otherwise the plan can suggest skills the
            agent has no way to load.

    Returns:
        A string containing exactly one <plan>...</plan> block, ready to
        prepend to the customer message before invoking the main agent.
    """
    tools = tools_catalogue if tools_catalogue is not None else _tools_catalogue_fallback()
    if skills_enabled:
        skills = skills_catalogue if skills_catalogue is not None else "(no skills catalogue provided)"
    else:
        skills = None  # sentinel: drop the skills section entirely
    policies = policies_catalogue if policies_catalogue is not None else _policies_catalogue()

    client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _planner_system_prompt(tools, skills, policies)},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
    )
    return (response.choices[0].message.content or "").strip()
