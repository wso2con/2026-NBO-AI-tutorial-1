"""Shared policy-KB retrieval — same code path for v1 and v2.

Both agents read from the same `policies/*.md` files using the same
keyword-scored matcher and return the same shape. The DIFFERENCE
between the two agents is in framing, not substance: v1 exposes this
via `search_kb` with a one-line docstring; v2 exposes it via
`search_policy_kb` over MCP with a docstring that says "always call
this BEFORE compensating actions" and a `handle-refund` skill that
names the procedure. Same evidence on the table; different ergonomics
push the LLM toward different first moves. That's the §6
Plan-before-commit lesson — telling the LLM to consult policy isn't
the same as wiring it to consult policy.

The retrieval itself is deliberately simple: parse frontmatter,
score by `keywords` (2 pts) + title word overlap (1 pt), keep the
top 3. Same scoring on both sides keeps the variable isolated.
"""

from __future__ import annotations

import re
from pathlib import Path

POLICIES_DIR = Path(__file__).parent


def _parse_policy(path: Path) -> dict:
    """Parse a policy markdown file. Returns id, title, keywords, rule, full_text.

    Format: YAML-ish frontmatter + markdown body. We extract frontmatter
    by hand instead of pulling in a YAML dep just for this.
    """
    text = path.read_text()

    fm_match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    if not fm_match:
        return {
            "id": path.stem,
            "title": path.stem.replace("_", " ").title(),
            "keywords": [],
            "rule": text.strip(),
            "full_text": text.strip(),
        }

    fm_text, body = fm_match.groups()
    fm: dict = {}
    for line in fm_text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            value = [v.strip() for v in value[1:-1].split(",") if v.strip()]
        fm[key] = value

    return {
        "id": fm.get("id", path.stem),
        "title": fm.get("title", path.stem),
        "keywords": fm.get("keywords", [])
        if isinstance(fm.get("keywords"), list)
        else [],
        "rule": body.strip(),
        "full_text": text.strip(),
    }


def _load_policies() -> list[dict]:
    """Read every .md in policies/, parsed. Empty list if the dir is missing."""
    if not POLICIES_DIR.exists():
        return []
    return [_parse_policy(p) for p in sorted(POLICIES_DIR.glob("*.md"))]


def search(query: str, top_k: int = 3) -> list[dict]:
    """Keyword + title-overlap scoring; top_k matches.

    Returns a list of `{id, title, rule, relevance}` dicts. If nothing
    scored above zero, returns a single `no_match` placeholder pointing
    the caller at escalation — same shape so consumers don't branch on
    "empty vs full" responses.
    """
    policies = _load_policies()
    q = query.lower()
    matches: list[tuple[int, dict]] = []

    for p in policies:
        score = 0
        for kw in p["keywords"]:
            if isinstance(kw, str) and kw.lower() in q:
                score += 2
        for word in p["title"].lower().split():
            if word in q and len(word) > 3:
                score += 1
        if score > 0:
            matches.append((score, p))

    matches.sort(key=lambda x: x[0], reverse=True)

    if not matches:
        return [
            {
                "id": "no_match",
                "title": "No matching policy",
                "rule": (
                    "No policy in the knowledge base matched this query. "
                    "If unsure how to proceed, escalate to a human."
                ),
                "relevance": 0,
            }
        ]

    return [
        {
            "id": p["id"],
            "title": p["title"],
            "rule": p["rule"],
            "relevance": score,
        }
        for score, p in matches[:top_k]
    ]
