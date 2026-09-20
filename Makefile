# Inside the Agent Loop — side-by-side agent comparison.
#
# Two FastAPI services and a React frontend, all started with one command.
#
#   make install   first-time setup: per-agent venvs + npm deps
#   make dev       run all three services (Ctrl-C stops everything)
#
# Individual targets if you want one service at a time for debugging:
#   make v1 / make v2 / make web
#
# Maintenance:
#   make reset     restore mocks from seeds, clear non-seeded memory
#   make clean     remove venvs, node_modules, build artifacts

SHELL := /bin/bash

.PHONY: help install dev v1 v2 web test reset clean

help:
	@echo "Targets:"
	@echo "  make install   — create per-agent venvs, install Python + Node deps"
	@echo "  make dev       — start v1 (:8001), v2 (:8002), web (:5173) together"
	@echo "  make v1        — start cs_agent_v1 only"
	@echo "  make v2        — start cs_agent_v2 only"
	@echo "  make web       — start the React frontend only"
	@echo "  make test      — run loop-engineering tests + production web build"
	@echo "  make reset     — restore mocks from seeds, wipe non-seeded memory"
	@echo "  make clean     — remove venvs, node_modules, build artifacts"

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

install:
	@echo "→ cs_agent_v1: venv + deps"
	cd cs_agent_v1 && python3 -m venv .venv && .venv/bin/pip install -q -U pip && .venv/bin/pip install -q .
	@echo "→ cs_agent_v2: venv + deps"
	cd cs_agent_v2 && python3 -m venv .venv && .venv/bin/pip install -q -U pip && .venv/bin/pip install -q .
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
	  (cd cs_agent_v1 && .venv/bin/uvicorn main:app --port 8001 --log-level warning) & \
	  (cd cs_agent_v2 && .venv/bin/uvicorn main:app --port 8002 --log-level warning) & \
	  (cd web && npm run dev) & \
	  wait

v1:
	cd cs_agent_v1 && .venv/bin/uvicorn main:app --port 8001 --reload

v2:
	cd cs_agent_v2 && .venv/bin/uvicorn main:app --port 8002 --reload

web:
	cd web && npm run dev

test:
	cs_agent_v2/.venv/bin/python -m unittest discover -s tests -v
	cd web && npm run build

# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------

# reset.py restores mocks/data/ from seeds and clears non-seeded memory.
# The web UI's "reset" button hits each service's /api/reset; use this
# target when the services aren't running (e.g. between rehearsals).
reset:
	python3 reset.py

clean:
	rm -rf cs_agent_v1/.venv cs_agent_v2/.venv web/node_modules
	rm -rf cs_agent_v1/build cs_agent_v1/*.egg-info
	rm -rf cs_agent_v2/build cs_agent_v2/*.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
