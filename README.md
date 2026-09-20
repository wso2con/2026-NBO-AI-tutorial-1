# Inside the Agent Loop

Two customer-support agents running side-by-side against the same prompt. Same model, same customer message, different harness around the LLM. The diff is the lesson.

## The four parts

The lab runs as three processes plus a shared execution-state and validation layer:

- **`cs_agent_v1/`** — the **first-cut** customer-support agent. The kind of thing a competent team ships in week one: identity, refund cap, procedure, and tool list all live in a Python file and the system prompt. Tools are imported in-process and shaped like real internal APIs (one god-tool that does cancel + refund + address change, free-text errors, SOAP-styled responses, atomic micro-getters). One shared `Agent` instance serves every customer. No skills, no MCP, no harness hooks, no episodic memory. v1 is not stupid; it's just what happens when you don't yet know which seams will matter.

- **`cs_agent_v2/`** — the **improved version**. The same identity and authority live in a declarative `agent-profile.yaml`. Tools are scoped MCP services with typed parameters and structured errors. A `skills/` directory carries procedural know-how and a colocated task contract for validation. Harness hooks bind customer identity, enforce the refund cap, meter real tool dispatches, and capture the exact input before every model call. A per-customer agent cache plus per-customer episodic memory files give continuity. An optional pre-LLM planner separates intent recognition from tool selection.

- **`web/`** — the **comparison UI**. Connects to both agents over HTTP, fans the same prompt out to both in parallel, and renders the two SSE streams side by side. Lets you swap models, customers, and the v2 feature toggles (skills / memory / planner) mid-demo. The merge happens in the browser; there's no dispatcher in the middle.

- **`loop_state.py` and `evaluations.py`** — the explicit harness state and deterministic validation layer. Live success criteria and ordering rules are registered from the Skill the model actually loads, never from the selected demo scenario. Evaluation expectations remain outside both agents. The engineered loop buffers each proposed reply and releases it only after the active task contract passes.

**Same model. Same prompt. The differences are everything around the LLM.**

### Context, memory, state, and observations

The demo keeps four often-confused concepts separate:

- **Context** is the temporary package assembled for one model invocation: instructions, current messages, selected memory, an optional plan, loaded Skill content, tool contracts, and accumulated observations. A pre-model hook exposes that exact package as `context_iteration`.
- **Memory** is durable stored information that may survive a session. The harness selects some memory into a later call's context; the store itself is not the context.
- **Execution state** records progress for the current run: success criteria, completed steps, blockers, budgets, operations, and the next loop decision.
- **Backend observations** are external evidence returned by tools. The loop uses them to update execution state and verify completion.

The scenario drawer is a presenter convenience only. It fills the customer and prompt but sends no scenario ID or expected behavior to either agent.

---

## Prerequisites

Install these before running the steps below:

- **Python 3.11+** — `python3 --version`
  - macOS: `brew install python@3.11`
  - Ubuntu/Debian: `sudo apt install python3.11 python3.11-venv`
  - Windows: install from [python.org](https://www.python.org/downloads/) or use WSL2
- **Node 18+** and **npm** — `node --version`
  - macOS: `brew install node`
  - Ubuntu/Debian: `sudo apt install nodejs npm`
  - Windows: install from [nodejs.org](https://nodejs.org/) or use WSL2
- **`make`** — `make --version`
  - macOS: included with Xcode CLT (`xcode-select --install`)
  - Ubuntu/Debian: `sudo apt install build-essential`
  - Windows: use **WSL2** or **Git Bash** (the Makefile uses bash features that don't run in PowerShell)
- **An OpenAI API key** with access to `gpt-5.4-mini` (or another supported model — see the model dropdown in the UI)

---

## Setup

```bash
cd inside-the-agent-loop
make install
```

`make install` does:

- Creates `cs_agent_v1/.venv` and installs its Python deps from `cs_agent_v1/pyproject.toml`
- Creates `cs_agent_v2/.venv` and installs its Python deps from `cs_agent_v2/pyproject.toml`
- Runs `npm install` in `web/`
- Copies `.env.example` to `.env` if it doesn't exist yet

Then open `.env` and paste your key:

```
OPENAI_API_KEY=sk-...
```

One `.env` covers the whole lab — both agent services read it.

---

## Run

```bash
make dev
```

Three processes come up in parallel:

| Process | Port | URL |
|---|---|---|
| `cs_agent_v1` | `:8001` | http://localhost:8001 |
| `cs_agent_v2` | `:8002` | http://localhost:8002 |
| `web` | `:5173` | http://localhost:5173 |

Open **<http://localhost:5173>** in your browser. Ctrl-C in the terminal stops all three together.

The presenter controls in the header include:

- **Tool budget** — sets the maximum number of tool invocations for the next turn. Parallel tool calls each consume one unit.
- **End session** — clears conversation history while preserving scoped episodic memory.
- **Evaluate** — executes the validation suite and exposes the evidence behind every result.

The scenario drawer follows the presentation sequence:

- **A useful tool observation** compares a noisy legacy refund envelope with an action-oriented observation that can cleanly enter the next model call's context.
- **Address change across open orders** tests whether the engineered agent loads the task-specific Skill, inspects related orders, partitions them by status, and asks before broader action.
- **Cancel and calculate the net refund** verifies the $100 − 10% prior credit − 10% cancellation fee calculation and the required cancel-before-refund write order.
- **Budget pressure / graceful pause** compares a hard tool-call budget failure with a 90% guard that exits `PAUSE`, retains completed work, and resumes with a fresh turn budget.
- **Timeout recovery evaluation** is an evaluator-owned backend fault test. The live agents receive no hidden timeout behavior.
- **Missing address / ask-resume** compares natural multi-turn behavior without a scenario-specific branch in either service.
- **Damaged item / missing evidence** catches a plausible refund answer that skipped the required policy, photo request, or return-label step. A refund attempted before photo evidence becomes an explicit validation violation.
- **Late-order credit / incomplete checks** withholds the answer until order status, policy, existing refunds, and the successful credit write have all been observed in the required order.

The validation suite also covers promise continuity across sessions, customer-scoped consequential memory, conditional planning, identity binding, and recovery. Its expected deterministic summary is **first-cut 1/13** and **engineered 13/13**.

### Running one service at a time

Useful for debugging a single agent:

```bash
make v1     # cs_agent_v1 only (with --reload)
make v2     # cs_agent_v2 only (with --reload)
make web    # frontend only
```

---

## Reset between runs

The web UI's **reset** button hits both services' `/api/reset` endpoints — restores mocks from seeds, wipes conversation memory, clears non-seed episodic memory, and restores the turn budget to 12 calls. **End session** also restores that controller to 12.

When the services aren't running:

```bash
make reset
```

If Bob's episodic memory was modified by `compact_memory()` during a demo:

```bash
git restore cs_agent_v2/memory/episodic/customer_cust_002.md
```

## Verify before presenting

```bash
make test
```

This runs the deterministic state/recovery/evaluation tests and a production frontend build. It does not call the model API.

---

## Clean

```bash
make clean
```

Removes both venvs, `web/node_modules`, and build artifacts. Re-run `make install` afterward.

---

## Troubleshooting

- **`make dev` says port 5173 is in use.** Vite walks up the range (`5174`, `5175`, …). Both agents' CORS allowlists cover `:5170`–`:5189`, so any in-range port works.
- **Port 8001 / 8002 is in use.** `lsof -i :8001` (or `:8002`) to find what's bound. Kill it, or change the ports in the `Makefile` and the `AGENTS` entries in `web/src/lib/api.ts`.
- **`command not found: bash` on Windows.** Install **Git Bash** or switch to **WSL2** and re-run.
- **`No such file or directory: 'python'`** when an agent starts. The MCP subprocess can't find `python` on `PATH`. `cs_agent_v2/agent/core.py` substitutes `sys.executable` for `python` / `python3` in the MCP server config — if you still see this, you're on a non-standard Python install; make sure `python3` resolves and the venv was created cleanly.
- **OpenAI auth / 401 errors.** Confirm `OPENAI_API_KEY` is set in `.env` and the key has access to the model selected in the UI (default `gpt-5.4-mini`).
- **CORS errors in the browser console.** Confirm the web is on `:5170`–`:5189`. The CORS regex in `cs_agent_v*/main.py` covers that range.

---

## Repo layout

```
.
├── cs_agent_v1/        First-cut agent service (port 8001)
├── cs_agent_v2/        Production-shaped agent service (port 8002)
├── mocks/              Shared mock backend (Customer / Order / Ledger + seeds)
├── policies/           Shared policy docs (markdown)
├── loop_state.py       Explicit run state and structured loop events
├── evaluations.py      Deterministic outcome, trajectory, and release-gate checks
├── web/                React + Vite + Tailwind frontend (port 5173)
├── tests/              Loop-engineering tests
├── Makefile            make install / dev / v1 / v2 / web / reset / clean
├── reset.py            Reset script used when services aren't running
├── .env.example        Copy to .env, paste OPENAI_API_KEY
└── README.md           This file
```
