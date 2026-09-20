"""Scoped agent identity.

The agent has its OWN identity (not the user's). `refund_cap_usd` is the
scope ceiling enforced by `can_refund()` — `issue_refund` uses it to return
a structured policy_violation rather than process an out-of-bounds write.
In production this identity would be issued by your IDP as a scoped service
principal; here it's loaded from agent-profile.yaml.
"""

from dataclasses import dataclass


@dataclass
class AgentIdentity:
    agent_id: str
    name: str
    refund_cap_usd: float = 200.0

    def can_refund(self, amount_usd: float) -> tuple[bool, str | None]:
        """Returns (allowed, reason_if_not). The reason becomes the
        `detail` field of the structured 403 response from issue_refund."""
        if amount_usd <= 0:
            return False, f"refund amount must be positive, got ${amount_usd:.2f}"
        if amount_usd > self.refund_cap_usd:
            return False, (
                f"refund of ${amount_usd:.2f} exceeds agent cap of "
                f"${self.refund_cap_usd:.2f}"
            )
        return True, None
