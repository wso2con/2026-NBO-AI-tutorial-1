"""Pydantic models for the mock backend.

These shapes are what the client returns; tools call `model_dump()` on them
to hand structured dicts to the agent.
"""

from pydantic import BaseModel


class Customer(BaseModel):
    customer_id: str
    name: str
    email: str
    phone: str
    tier: str  # standard | business | premium
    verified: bool
    joined: str  # ISO date
    lifetime_orders: int  # rough loyalty signal


class Order(BaseModel):
    order_id: str
    customer_id: str
    status: str  # placed | preparing | in_transit | in_transit_delayed |
    # delivered | delivered_damaged | cancelled
    placed: str  # ISO date
    items: list[str]
    total_usd: float
    shipping_address: str
    carrier: str | None = None
    tracking_id: str | None = None
    estimated_delivery: str | None = None
    delivery_days_late: int = 0
    damaged: bool = False
    payment_method: str = ""


class LedgerEntry(BaseModel):
    kind: str  # refund | credit | cancel | address_update | ticket_<priority>
    order_id: str | None = None
    customer_id: str | None = None
    amount_usd: float | None = None
    # Refund-only: the fraction of order.total_usd that was refunded (e.g. 0.10
    # for a 10% shipping-delay credit). Recorded so the agent can SUM prior
    # percentages on an order directly, without redoing the amount/total math.
    refund_percentage: float | None = None
    reason: str = ""
    actor_agent_id: str = ""
    timestamp: str = ""
