# Inside the Agent Loop

**WSO2Con 2026 tutorial material.** Hands-on lab. Everything here runs locally against mock backends; no WSO2 or customer systems are involved, and all customer records in `mocks/seeds/` are fictional.

Two customer-support agents running side-by-side against the same prompt. Same model, same customer message, different harness around the LLM. The diff is the lesson.

### What you'll take away

Most of what separates a demo agent from a production one sits outside the model: how tools are shaped, where identity is bound, what the loop remembers, when it stops to ask, and how failure is handled. This lab makes each of those a toggle you can flip mid-run and watch the two agents diverge.

## The parts

The lab runs as three processes plus a set of shared lab-root modules and a post-turn LLM review layer:

- **`cs_agent_first_cut/`** — the **first-cut** customer-support agent. The kind of thing a competent team ships in week one: identity, refund cap, procedure, and tool list all live in a Python file and the system prompt. Tools are imported in-process and shaped like real internal APIs (one god-tool that does cancel + refund + address change, free-text errors, SOAP-styled responses, atomic micro-getters). One shared `Agent` instance serves every customer. No skills, no MCP, no harness hooks, no episodic memory. The first-cut agent is not stupid; it's just what happens when you don't yet know which seams will matter.

- **`cs_agent_engineered/`** — the **improved version**. The same identity and authority live in a declarative `agent-profile.yaml`. Tools are scoped MCP services with typed parameters and structured errors. A `skills/` directory carries procedural know-how and a colocated task contract that the LLM reviewer can inspect. Harness hooks bind customer identity, enforce the refund cap, meter real tool dispatches, and capture the exact input before every model call. A per-customer agent cache plus per-customer episodic memory files give continuity. It can also run the shared pre-LLM planner, which separates intent recognition from tool selection.

- **`web/`** — the **comparison UI**. Connects to both agents over HTTP, fans the same prompt out to both in parallel, and renders the two SSE streams side by side. Lets you swap models, customers, and the feature toggles (skills / memory on the engineered side; planner on both), plus arm a one-shot refund-service timeout. The merge happens in the browser; there's no dispatcher in the middle.

- **Lab-root modules** — `planner.py`, `run_control.py` and `context_trace.py` are shared by BOTH agents, which put the repo root on `sys.path` and import from it. Neither agent depends on the other. The planner in particular is a harness pattern, not an engineered-only feature: it is available on both panels (with skills disabled on the first-cut side, which has no skills loader) and is **off by default** on both — flip it per panel in the UI.

- **`loop_state.py` and `policy_evaluator.py`** — shared run evidence and three independent post-turn LLM reviews: policy compliance, groundedness, and execution path. Both agents are judged from the customer request, observed tool trajectory, policies, and any task contract the agent actually loaded. Reviews annotate completed replies; they never gate them.

**Same model. Same prompt. The differences are everything around the LLM.**

### Context, memory, state, and observations

The demo keeps four often-confused concepts separate:

- **Context** is the temporary package assembled for one model invocation: instructions, current messages, selected memory, an optional plan, loaded Skill content, tool contracts, and accumulated observations. A pre-model hook exposes that exact package as `context_iteration`.
- **Memory** is durable stored information that may survive a session. The harness selects some memory into a later call's context; the store itself is not the context.
- **Execution state** records progress for the current run: completed steps, blockers, budgets, operations, and the next loop decision.
- **Backend observations** are external evidence returned by tools. The loop records them for subsequent model calls, controls, and review.

The scenario drawer sends no scenario ID or expected behavior to either agent. It normally only fills the customer and prompt; the recovery scenario also arms the same explicit one-shot fault control the presenter can toggle in the header.

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
git clone https://github.com/wso2con/2026-NBO-AI-tutorial-1.git
cd 2026-NBO-AI-tutorial-1
make install
```

`make install` does:

- Creates `cs_agent_first_cut/.venv` and installs its Python deps from `cs_agent_first_cut/pyproject.toml`
- Creates `cs_agent_engineered/.venv` and installs its Python deps from `cs_agent_engineered/pyproject.toml`
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
| `cs_agent_first_cut` | `:8001` | http://localhost:8001 |
| `cs_agent_engineered` | `:8002` | http://localhost:8002 |
| `web` | `:5173` | http://localhost:5173 |

Open **<http://localhost:5173>** in your browser. Ctrl-C in the terminal stops all three together.

The presenter controls in the header include:

- **Total-token budget** — sets the chat session's loop budget, counted as input plus output tokens. Spend accumulates across messages in the same visible chat, and the ceiling grows by one grant each time the customer approves a continuation (X, 2X, 3X…). Planner, reviewer, wrap-up, and the policy MCP's one-time internal lookup are outside this demo meter. Every reply shows cumulative total/limit, the input/output split, and model calls for that turn; a small chart shows the real input context sent to each model call.
- **Auto-compact at** (engineered only) — the context line at which the harness summarizes the oldest messages instead of letting the next call's context keep growing, measured in the same projected input tokens the context chart is drawn in. `off` leaves the session's message window (`memory.session.window`) as the only bound; any other value replaces that cap with summarization at the chosen line. Tool observations are never truncated: the pipeline drops Strands' `context_manager="auto"` tool-result clipping and keeps only message-level summarization, so what the model reads is either the backend's own bytes or a summary that says so. The summarization call is harness work, like the planner and the reviewer, and is outside the token budget. Compactions are announced in the trace and marked in the context chart under the reply.
- **End session** — clears conversation history while preserving scoped episodic memory.

The scenario drawer follows the presentation sequence:

- **A useful tool observation** compares a noisy legacy refund envelope with an action-oriented observation that can cleanly enter the next model call's context.
- **Address change across open orders** tests whether the engineered agent loads the task-specific Skill, inspects related orders, partitions them by status, and asks before broader action.
- **Cancel and calculate the net refund** verifies the $100 − 10% prior credit − 10% cancellation fee calculation and the required cancel-before-refund write order.
- **Human decisions in the loop** — both places the engineered agent stops short of acting ship the same `pause` block on the `done` event, and the console renders both as one inline control with the decision's own labels. Neither answer is ever read out of the customer's prose by the model: `write_confirmation_required` (a write tool queued behind `HumanConfirmationHook`, answered with `confirm`) offers **Proceed / Don't do it** and lists the exact call it is holding; `budget_grant_required` (the token guard, answered with `budget_grant`) offers **Continue / Stop**. A typed message still works for a confirmation, where anything that is not a clear yes is safely a no, and is deliberately not accepted for the budget pause, where the fail-closed reading of an unrelated message is "this is a new request".
- **Budget pressure / graceful pause** compares a hard token-budget failure with a 90% guard that *suspends* the loop rather than ending it. Session memory is left exactly where the loop stopped, on the observation the withheld model call was about to read. The reserved call then goes to a tool-free wrap-up that reads that same session memory and tells the customer where things stand. Continue re-enters the same run with one more grant and resumes with `stream_async(prompt=None)`, adding nothing to the conversation: the wrap-up text is a side channel and is thrown away. Stop clears the pause via `/api/budget_stop` without running the agent.
- **Missing address / ask-resume** compares natural multi-turn behavior without a scenario-specific branch in either service.
- **Refund service timeout** arms a one-shot pre-commit fault. First-cut leaks the raw transport exception as an unstructured tool error; engineered normalizes it into a structured, non-retryable `service_timeout`, escalates for manual handling, and does not claim the refund succeeded.
- **Damaged item / missing evidence** shows the hard refund-evidence control and lets the LLM judge explain whether the reply followed the observed policy and tool path.
- **Late-order credit / incomplete checks** lets the LLM judge compare the reply with the order, policy, refund history, and successful write in the recorded trajectory.

### Running one service at a time

Useful for debugging a single agent:

```bash
make first-cut    # cs_agent_first_cut only (with --reload)
make engineered   # cs_agent_engineered only (with --reload)
make web    # frontend only
```

---

## Reset between runs

The web UI's **reset** button hits both services' `/api/reset` endpoints — restores mocks from seeds, wipes conversation memory, clears non-seed episodic memory, and restores the session budget to 40,000 total tokens. **End session** also starts a fresh 40,000-token meter.

When the services aren't running:

```bash
make reset
```

If Bob's episodic memory was modified by `compact_memory()` during a demo:

```bash
git restore cs_agent_engineered/memory/episodic/customer_cust_002.md
```

## Verify before presenting

```bash
make test
```

This runs the unit tests for runtime controls and LLM-review plumbing, then builds the production frontend. Mocked reviewer tests do not call the model API.

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
- **`No such file or directory: 'python'`** when an agent starts. The MCP subprocess can't find `python` on `PATH`. `cs_agent_engineered/agent/core.py` substitutes `sys.executable` for `python` / `python3` in the MCP server config — if you still see this, you're on a non-standard Python install; make sure `python3` resolves and the venv was created cleanly.
- **OpenAI auth / 401 errors.** Confirm `OPENAI_API_KEY` is set in `.env` and the key has access to the model selected in the UI (default `gpt-5.4-mini`).
- **CORS errors in the browser console.** Confirm the web is on `:5170`–`:5189`. The CORS regex in `cs_agent_first_cut/main.py` and `cs_agent_engineered/main.py` covers that range.

---

## Repo layout

```
.
├── cs_agent_first_cut/   First-cut agent service (port 8001)
├── cs_agent_engineered/  Engineered agent service (port 8002)
├── mocks/                Shared mock backend (Customer / Order / Ledger + seeds)
├── policies/             Shared policy docs (markdown)
├── web/                  React + Vite + Tailwind frontend (port 5173)
├── tests/                Loop-engineering tests
│
│   Lab-root modules — both agents put this directory on sys.path and
│   import from it, so neither agent depends on the other:
├── loop_state.py         Explicit run state and structured loop events
├── run_control.py        TokenBudgetHook — shared session token metering
├── budget_wrapup.py      The one tool-free model call a paused turn is allowed;
│                      reads session memory, output never re-enters it
├── context_trace.py      ContextTraceHook — pre-model-call context capture
├── planner.py            Shared pre-LLM planner (agent-agnostic; off by default)
├── policy_evaluator.py   Shared post-turn LLM reviewers for both agents
├── session_view.py       Read-only view of a run's conversation and trace
├── demo_clock.py         Pinned demo clock, so dated scenarios stay reproducible
├── reset.py              Reset script used when services aren't running
│
├── Makefile              make install / dev / first-cut / engineered / web / reset / clean
├── .env.example          Copy to .env, paste OPENAI_API_KEY
└── README.md             This file
```
