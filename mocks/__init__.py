"""Mock backend used by both cs_agent_v1 and cs_agent_v2.

CustomerSupportClient is constructed with an `agent_id` and reads/writes
its own files under `mocks/data/<agent_id>/`. The canonical seeds at
`mocks/seeds/*.json` are shared and read-only — both agents start from
the same fixtures, then evolve their own independent worlds. Reset by
calling `client.reset()` (or `reset_data_files(agent_id)`); only that
agent's data is touched.
"""

from mocks.client import CustomerSupportClient
from mocks.models import Customer, LedgerEntry, Order

__all__ = ["CustomerSupportClient", "Customer", "LedgerEntry", "Order"]
