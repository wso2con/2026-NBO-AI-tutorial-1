"""Transport selection shared by both MCP servers in this package.

These servers were written for stdio: `agent/core.py` spawns each one as a
subprocess and talks to it over a pipe, so there was no address and no caller
but the agent itself. `serve()` keeps that the default and adds HTTP
transports, so the same server can also be reached over a network.

    python -m mcp_servers.customer_support_engineered
        stdio, exactly as before -- this is what agent/core.py invokes

    python -m mcp_servers.customer_support_engineered --transport http
        streamable HTTP on 0.0.0.0:8765/mcp, open to any caller

Settings may also come from the environment (MCP_TRANSPORT, MCP_HOST,
MCP_PORT), which is how you configure the server when the command line
belongs to a process manager or container rather than to you.

The HTTP mode here is deliberately wide open: no token, any Host header, any
CORS origin, so a page served from any localhost port -- or any other origin
-- can call it from the browser during a demo. Every caller gets the write
tools too. For `customer_support` the blast radius is the mock JSON under
`mocks/data/`, which `make reset` restores. For `policy_kb` it is your OpenAI
bill, since each `check_policy` call spends real credits.
"""

from __future__ import annotations

import argparse
import os
import json
import logging
import socket
import sys
import time

DEFAULT_PORT = 8765

_log = logging.getLogger("mcp_servers.trace")

# 8001 and 8002 are the two FastAPI services (see the Makefile) and 8000 is
# FastMCP's own default, so the servers start clear of all three.


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _fmt_args(arguments: object) -> str:
    if not isinstance(arguments, dict):
        return _clip(repr(arguments), 200)
    return ", ".join(f"{k}={_clip(json.dumps(v, default=str), 60)}" for k, v in arguments.items())


def _fmt_result(result: object) -> str:
    """Summarize a tool result, leading with the error when there is one.

    These servers return failures as ordinary dicts rather than raising, so a
    trace that only said "returned dict" would hide the single most useful
    fact about the call. Pull `error` to the front instead.
    """
    payload = result
    # By the time a call reaches here it has usually been converted, so the
    # dict the tool returned is wrapped: a (content, structured) pair, or a
    # list of content blocks whose text is the JSON. Unwrap both, or the
    # trace degrades into a repr of the transport's own plumbing.
    if isinstance(payload, tuple) and len(payload) == 2:
        payload = payload[1] if isinstance(payload[1], dict) else payload[0]
    if isinstance(payload, (list, tuple)):
        texts = [getattr(block, "text", None) for block in payload]
        joined = "".join(text for text in texts if text)
        if joined:
            try:
                payload = json.loads(joined)
            except ValueError:
                payload = joined
    if isinstance(payload, dict):
        if "error" in payload:
            detail = payload.get("detail") or payload.get("remediation") or ""
            code = f" {payload['code']}" if "code" in payload else ""
            return _clip(f"ERROR {payload['error']}{code} {detail}".strip(), 220)
        return _clip(json.dumps(payload, default=str), 220)
    return _clip(repr(payload), 220)


def trace_tool_calls(mcp) -> None:
    """Log every tool call: name, arguments, outcome and duration.

    Wraps the tool manager, which is the one chokepoint every invocation
    passes through, so no tool can be added later and quietly escape the
    trace.

    Everything goes to stderr, via logging's default handler. That is not
    incidental: under stdio the protocol itself owns stdout, and a single
    print() there would corrupt the stream, so tracing must never be the
    thing that breaks the transport it is meant to debug.
    """
    manager = mcp._tool_manager
    original = manager.call_tool

    async def traced(name, arguments, context=None, convert_result=False):
        _log.debug("→ %s(%s)", name, _fmt_args(arguments))
        started = time.perf_counter()
        try:
            result = await original(name, arguments, context=context, convert_result=convert_result)
        except Exception as exc:
            elapsed = (time.perf_counter() - started) * 1000
            _log.debug("← %s raised %s: %s [%.0fms]", name, type(exc).__name__, exc, elapsed)
            raise
        elapsed = (time.perf_counter() - started) * 1000
        _log.debug("← %s %s [%.0fms]", name, _fmt_result(result), elapsed)
        return result

    manager.call_tool = traced


def enable_debug_logging() -> None:
    """Turn on tool tracing and the MCP framework's own debug output.

    The server modules quiet these loggers at import, so that a demo trace is
    not buried in framework chatter. Undo that here rather than there, so the
    quiet default survives for everyone who did not ask for this.
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)-5s [%(name)s] %(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.DEBUG)
    for name in ("mcp", "mcp.server", "mcp.server.lowlevel", "FastMCP", "mcp_servers.trace"):
        logging.getLogger(name).setLevel(logging.DEBUG)


def lan_ip() -> str | None:
    """Best guess at this machine's LAN address.

    Connecting a UDP socket transmits nothing. It only asks the routing table
    which local interface would carry traffic toward that destination.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.connect(("192.0.2.1", 53))  # TEST-NET-1, never routed
            return sock.getsockname()[0]
        except OSError:
            return None


def _env_default(name: str, fallback):
    value = os.environ.get(name)
    return value if value not in (None, "") else fallback


def build_parser(server_key: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"python -m mcp_servers.{server_key}",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--transport",
        default=_env_default("MCP_TRANSPORT", "stdio"),
        choices=("stdio", "http", "sse"),
        help="stdio (default, what the agent uses), http (streamable HTTP), or sse (deprecated)",
    )
    parser.add_argument(
        "--host",
        default=_env_default("MCP_HOST", "0.0.0.0"),
        help="bind address for http/sse (default: 0.0.0.0, all interfaces)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(_env_default("MCP_PORT", DEFAULT_PORT)),
        help=f"bind port for http/sse (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--stateless",
        action="store_true",
        default=_env_default("MCP_STATELESS", "") not in ("", "0", "false"),
        help="do not keep per-client sessions; each request stands alone "
        "(useful behind proxies and for many short-lived demo clients)",
    )
    parser.add_argument(
        "--json-response",
        action="store_true",
        help="answer with plain JSON instead of an SSE stream (simpler for curl and basic clients)",
    )
    parser.add_argument(
        "--log-level",
        default=_env_default("MCP_LOG_LEVEL", "warning"),
        choices=("critical", "error", "warning", "info", "debug", "trace"),
        help="uvicorn log level (default: warning). Use `info` to see the access log — "
        "which client called what, and how each request was answered. Worth reaching "
        "for whenever a client misbehaves, since at the default level a failing request "
        "leaves no trace and only uvicorn's own warnings appear.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=_env_default("MCP_DEBUG", "") not in ("", "0", "false"),
        help="trace every tool call — arguments, result or error, and duration — "
        "plus the MCP framework's own debug output. Writes to stderr, so it is "
        "safe under stdio as well as http.",
    )
    return parser


def _cors(app):
    """Wrap the app so a browser on any origin may call it.

    Two separate gates stand between a browser client and this server, and
    both have to come down.

    The server's own gate is DNS-rebinding protection, which checks the Host
    and Origin headers against an allowlist. The `mcp` package only enforces
    it when `transport_security` is set, so the servers here leave it None.

    The browser's gate is CORS, and it needs more than permissive origins.
    `Mcp-Session-Id` must be named in `expose_headers`, because a cross-origin
    response only hands JavaScript a short list of safe headers by default and
    a streamable-HTTP client cannot continue its session without reading that
    one. The same header arrives on requests, so it has to be allowed inbound
    too. DELETE is listed because that is how a client ends its session.

    `allow_origins=["*"]` and credentials are mutually exclusive per the CORS
    spec; this server authenticates nobody, so there is nothing to send.
    """
    from starlette.middleware.cors import CORSMiddleware

    return CORSMiddleware(
        app,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["Mcp-Session-Id", "Mcp-Protocol-Version"],
    )


def _port_is_free(host: str, port: int) -> bool:
    bind_host = "" if host == "0.0.0.0" else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((bind_host, port))
        except OSError:
            return False
    return True


def serve(mcp, server_key: str, argv: list[str] | None = None) -> int:
    """Run `mcp` on the transport named by the command line or environment."""
    args = build_parser(server_key).parse_args(argv)

    if args.debug:
        enable_debug_logging()
        trace_tool_calls(mcp)

    if args.transport == "stdio":
        # Unchanged behaviour: a pipe to the parent process, no address.
        # Nothing may be printed to stdout here -- stdout is the protocol.
        mcp.run(transport="stdio")
        return 0

    if not _port_is_free(args.host, args.port):
        print(f"error: {args.host}:{args.port} is already in use -- pass --port.", file=sys.stderr)
        return 1

    mcp.settings.host = args.host
    mcp.settings.port = args.port
    mcp.settings.stateless_http = args.stateless
    mcp.settings.json_response = args.json_response
    # Leave DNS-rebinding protection off (see _cors) so any Host is accepted.
    mcp.settings.transport_security = None

    if args.transport == "http":
        app = mcp.streamable_http_app()
        path = mcp.settings.streamable_http_path
        cli_transport = "http"
    else:
        app = mcp.sse_app()
        path = mcp.settings.sse_path
        cli_transport = "sse"

    app = _cors(app)

    if args.host == "0.0.0.0":
        address = lan_ip() or "127.0.0.1"
    else:
        address = args.host
    url = f"http://{address}:{args.port}{path}"

    tool_names = sorted(tool.name for tool in mcp._tool_manager.list_tools())

    print()
    print(f"  {mcp.name}  [{args.transport}]")
    print(f"  URL:      {url}")
    print(f"  bind:     {args.host}:{args.port}" + ("   (all interfaces)" if args.host == "0.0.0.0" else ""))
    print("  auth:     none -- every caller can use every tool, including writes")
    print("  cors:     any origin")
    print(f"  tools:    {', '.join(tool_names)}")
    print()
    print("  Connect a client:")
    print(f"    claude mcp add --transport {cli_transport} {server_key} {url}")
    if args.host == "0.0.0.0":
        print(f"    (also on http://127.0.0.1:{args.port}{path} from this machine)")
    print()
    print("  Ctrl-C to stop.")
    print()
    sys.stdout.flush()

    import uvicorn

    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    except KeyboardInterrupt:
        print("\nstopped.")
    return 0
