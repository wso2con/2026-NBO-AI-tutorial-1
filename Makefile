# Inside the Agent Loop — side-by-side agent comparison.
#
# Two FastAPI services and a React frontend, all started with one command.
#
#   make install   first-time setup: per-agent venvs + npm deps
#   make dev       run all three services (Ctrl-C stops everything)
#
# Individual targets if you want one service at a time for debugging:
#   make first-cut / make engineered / make web
#
# Maintenance:
#   make reset     restore mocks from seeds, clear non-seeded memory
#   make clean     remove venvs, node_modules, build artifacts

SHELL := /bin/bash

.PHONY: help install dev first-cut engineered web test reset clean

help:
	@echo "Targets:"
	@echo "  make install   — create per-agent venvs, install Python + Node deps"
	@echo "  make dev       — start first-cut (:8001), engineered (:8002), web (:5173) together"
	@echo "  make first-cut — start cs_agent_first_cut only"
	@echo "  make engineered — start cs_agent_engineered only"
	@echo "  make web       — start the React frontend only"
	@echo "  make test      — run loop-engineering tests + production web build"
	@echo "  make reset     — restore mocks from seeds, wipe non-seeded memory"
	@echo "  make clean     — remove venvs, node_modules, build artifacts"

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

install:
	@echo "→ cs_agent_first_cut: venv + deps"
	cd cs_agent_first_cut && python3 -m venv .venv && .venv/bin/pip install -q -U pip && .venv/bin/pip install -q .
	@echo "→ cs_agent_engineered: venv + deps"
	cd cs_agent_engineered && python3 -m venv .venv && .venv/bin/pip install -q -U pip && .venv/bin/pip install -q .
	@echo "→ web: npm deps"
	cd web && npm install --silent
	@echo "→ .env"
	@if [ ! -f .env ]; then cp .env.example .env && echo "  created .env from .env.example"; else echo "  .env already exists, leaving as-is"; fi
	@echo ""
	@echo "✓ install complete"
	@echo ""
	@if [ -f .env ] && ! grep -q "^OPENAI_API_KEY=sk-[^.]" .env; then \
	  echo "  ⚠  paste your OpenAI key into .env  (OPENAI_API_KEY=sk-…)"; \
	  echo ""; \
	fi
	@echo "  Then: make dev"

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

# Start all three services in parallel. The trap kills the whole process
# group on Ctrl-C so you don't end up with orphaned uvicorn/vite processes.
dev:
	@trap 'kill 0' SIGINT SIGTERM; \
	  (cd cs_agent_first_cut && .venv/bin/uvicorn main:app --port 8001 --log-level warning --reload --reload-dir . --reload-dir ..) & \
	  (cd cs_agent_engineered && .venv/bin/uvicorn main:app --port 8002 --log-level warning --reload --reload-dir . --reload-dir ..) & \
	  (cd web && npm run dev) & \
	  wait

first-cut:
	cd cs_agent_first_cut && .venv/bin/uvicorn main:app --port 8001 --reload

engineered:
	cd cs_agent_engineered && .venv/bin/uvicorn main:app --port 8002 --reload

web:
	cd web && npm run dev

test:
	cs_agent_engineered/.venv/bin/python -m unittest discover -s tests -v
	cd web && npm run build

# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------

# reset.py restores mocks/data/ from seeds and clears non-seeded memory.
# The web UI's "reset" button hits each service's /api/reset; use this
# target when the services aren't running (e.g. between rehearsals).
# Run from the engineered venv: reset.py imports mocks.client, which pulls in
# pydantic, and the system python3 has no reason to have it.
reset:
	cs_agent_engineered/.venv/bin/python reset.py

clean:
	rm -rf cs_agent_first_cut/.venv cs_agent_engineered/.venv web/node_modules
	rm -rf cs_agent_first_cut/build cs_agent_first_cut/*.egg-info
	rm -rf cs_agent_engineered/build cs_agent_engineered/*.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
