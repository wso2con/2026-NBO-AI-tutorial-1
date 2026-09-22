#!/usr/bin/env bash
#
# run-mcp.sh — serve one of the engineered agent's MCP servers over HTTP.
#
#   ./run-mcp.sh                     customer_support on 0.0.0.0:8765/mcp
#   ./run-mcp.sh policy_kb           the policy advisor instead
#   ./run-mcp.sh --port 9000         extra flags pass straight through
#   ./run-mcp.sh --host 127.0.0.1    keep it on this machine
#   ./run-mcp.sh --no-debug          without the per-tool-call trace
#   ./run-mcp.sh --help              the full option list
#
# Serving here means serving clients you did not write, so this turns on the
# access log and a trace of every tool call by default. _serve.py leaves both
# off, because under the agent these servers are stdio subprocesses whose
# output nobody is watching.
#
# Run directly, the servers speak stdio, because that is what agent/core.py
# spawns them for. This script is for the other case: putting them on HTTP so
# a browser page, another agent, or a client on the LAN can reach them. So it
# adds `--transport http` unless you pass a --transport of your own.
#
# Every other flag is forwarded verbatim to `python -m mcp_servers.<server>`.
# The option set therefore lives in exactly one place, mcp_servers/_serve.py,
# and this script never falls out of date with it.
#
# Note what HTTP mode opens up: no authentication, any origin, and the write
# tools are live. For customer_support that reaches the mock JSON under
# mocks/data/, which `make reset` restores. For policy_kb it reaches your
# OpenAI account, since every check_policy call spends credits.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_DIR="$ROOT/cs_agent_engineered"
VENV_PY="$AGENT_DIR/.venv/bin/python"

# Friendly name -> module, as a function rather than an associative array:
# macOS still ships bash 3.2, where `declare -A` does not exist.
module_for() {
  case "$1" in
    customer_support) echo "mcp_servers.customer_support_engineered" ;;
    policy_kb)        echo "mcp_servers.policy_kb" ;;
    *) echo "error: unknown server '$1' (customer_support | policy_kb)" >&2; return 1 ;;
  esac
}

SERVER="customer_support"
ARGS=()
HAS_TRANSPORT=0
HAS_LOG_LEVEL=0
HAS_DEBUG=0

for arg in "$@"; do
  case "$arg" in
    customer_support|policy_kb)
      # A bare server name, only when it is the first positional.
      if [[ ${#ARGS[@]} -eq 0 ]]; then SERVER="$arg"; else ARGS+=("$arg"); fi
      ;;
    --transport|--transport=*)
      HAS_TRANSPORT=1
      ARGS+=("$arg")
      ;;
    --log-level|--log-level=*)
      HAS_LOG_LEVEL=1
      ARGS+=("$arg")
      ;;
    --debug|--no-debug)
      # --no-debug is this script's own: it suppresses the default below
      # rather than reaching _serve.py, which has no such flag.
      HAS_DEBUG=1
      [[ "$arg" == "--debug" ]] && ARGS+=("$arg")
      ;;
    -*)
      ARGS+=("$arg")
      ;;
    *)
      # A bare word that is not a server name is far more likely a typo than
      # something _serve.py wants; it takes no positionals. Say so here rather
      # than letting it surface as an argparse error about an unknown option.
      if [[ ${#ARGS[@]} -eq 0 ]]; then
        module_for "$arg" >/dev/null || exit 2
      fi
      ARGS+=("$arg")
      ;;
  esac
done

if [[ ! -x "$VENV_PY" ]]; then
  echo "error: no virtualenv at $AGENT_DIR/.venv" >&2
  echo "  run 'make install' first." >&2
  exit 1
fi

# The servers do not load .env themselves — under the agent they inherit the
# environment from the parent process (see make_mcp_client in agent/core.py).
# Standalone there is no parent, so load it here, matching main.py's order:
# the lab-root .env first, then the agent's own, which does not override it.
load_env() {
  local file="$1"
  [[ -f "$file" ]] || return 0
  set -a
  # shellcheck disable=SC1090
  source "$file"
  set +a
}
load_env "$ROOT/.env"
OPENAI_API_KEY="${OPENAI_API_KEY:-}"
load_env "$AGENT_DIR/.env"

if [[ "$SERVER" == "policy_kb" && ( -z "${OPENAI_API_KEY:-}" || "$OPENAI_API_KEY" == sk-... ) ]]; then
  echo "warning: OPENAI_API_KEY is not set, so check_policy will fail on every call." >&2
  echo "  paste your key into $ROOT/.env" >&2
  echo >&2
fi

if [[ $HAS_TRANSPORT -eq 0 ]]; then
  ARGS=("--transport" "http" "${ARGS[@]+"${ARGS[@]}"}")
fi

# Serving over HTTP means clients you did not write, so the access log and a
# trace of every tool call are worth their noise: between them they are the
# only record of what a client asked for and what it got back. _serve.py
# keeps both off by default, for the agent's stdio subprocesses where the
# output would be noise nobody asked for. Pass --no-debug or an explicit
# --log-level to opt back out.
if [[ $HAS_LOG_LEVEL -eq 0 ]]; then
  ARGS=("--log-level" "info" "${ARGS[@]+"${ARGS[@]}"}")
fi
if [[ $HAS_DEBUG -eq 0 ]]; then
  ARGS=("--debug" "${ARGS[@]+"${ARGS[@]}"}")
fi

# cd so `python -m mcp_servers.…` resolves the package from the current
# directory, and exec so Ctrl-C reaches the server rather than this shell.
cd "$AGENT_DIR"
exec "$VENV_PY" -m "$(module_for "$SERVER")" "${ARGS[@]+"${ARGS[@]}"}"
