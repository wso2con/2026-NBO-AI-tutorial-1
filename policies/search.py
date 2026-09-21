"""Shared policy-KB retrieval used by both demo agents.

The first-cut tool returns this retriever's top-three policy documents directly
to the customer-support model. The engineered policy-advisor MCP uses the same
retrieval result internally, then applies a specialist LLM and returns only a
case-specific decision brief. The corpus and candidate selection are therefore
shared; the context boundary is what differs.

Retrieval is deliberately simple: score by frontmatter keywords and title-word
overlap, then keep the top three. This makes the context-engineering comparison
visible without turning the lab into a search-ranking exercise.

Scoring alone is not enough, though, and deliberately staying dumb about
ranking is exactly why. These policies are a corpus, not a pile: a category
policy routinely delegates part of its answer to another document by naming it
in backticks (`cancellation` hands the arithmetic to `refund_calculation` and
the limit to `refund_authority`). A keyword scorer cannot see that, because the
words that would rank the delegated document are in the CUSTOMER'S question
only when the customer happens to use them. Ask "can I cancel this?" and the
document holding the percentage never surfaces, so the brief that comes back is
confidently silent about the money.

So `follow_references=True` reads the cross-references the policy authors
already wrote and pulls the cited documents in alongside the scored hits. It is
resolution, not ranking — the link is declared in the text, not inferred.
"""

from __future__ import annotations

import re
from pathlib import Path

POLICIES_DIR = Path(__file__).parent
_GENERIC_TITLE_WORDS = {
    "order",
    "orders",
    "item",
    "items",
    "customer",
    "customers",
    "policy",
    "when",
    "within",
}


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


def _referenced_ids(rule: str, known_ids: set[str]) -> list[str]:
    """Policy ids this document names in backticks, in order of appearance.

    Only ids that exist in the corpus are returned, so a renamed or mistyped
    reference resolves to nothing rather than to a fabricated candidate.
    """
    seen: list[str] = []
    for token in re.findall(r"`([a-z0-9_]+)`", rule):
        if token in known_ids and token not in seen:
            seen.append(token)
    return seen


def search(
    query: str,
    top_k: int = 3,
    follow_references: bool = False,
    max_total: int = 8,
) -> list[dict]:
    """Keyword + title-overlap scoring; top_k matches.

    Returns a list of `{id, title, rule, relevance}` dicts. If nothing
    scored above zero, returns a single `no_match` placeholder pointing
    the caller at escalation — same shape so consumers don't branch on
    "empty vs full" responses.

    With `follow_references`, any policy named in backticks by a scored hit is
    appended after the scored hits, carrying `relevance: 0` and a
    `referenced_by` field, up to `max_total` documents. The defaults leave the
    first-cut agent's tool exactly as it was: three scored documents, no
    expansion. The engineered advisor opts in, because expansion feeds the
    specialist model behind the context boundary and never the main agent.
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
            if word in q and len(word) > 3 and word not in _GENERIC_TITLE_WORDS:
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

    selected = [
        {
            "id": p["id"],
            "title": p["title"],
            "rule": p["rule"],
            "relevance": score,
        }
        for score, p in matches[:top_k]
    ]
    if not follow_references:
        return selected

    by_id = {p["id"]: p for p in policies}
    chosen_ids = {item["id"] for item in selected}
    for item in list(selected):
        if len(selected) >= max_total:
            break
        for ref_id in _referenced_ids(item["rule"], set(by_id)):
            if len(selected) >= max_total:
                break
            if ref_id in chosen_ids:
                continue
            ref = by_id[ref_id]
            chosen_ids.add(ref_id)
            selected.append(
                {
                    "id": ref["id"],
                    "title": ref["title"],
                    "rule": ref["rule"],
                    "relevance": 0,
                    "referenced_by": item["id"],
                }
            )
    return selected
