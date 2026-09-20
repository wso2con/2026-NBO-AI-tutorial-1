// SSE client over fetch + ReadableStream.
//
// We can't use the browser's native `EventSource` because it only supports
// GET. The agents accept POST with a JSON body, so we read the streamed
// response manually and parse SSE frames as they arrive.
//
// Frame format (per https://html.spec.whatwg.org/multipage/server-sent-events.html):
//   event: <type>\n
//   data: <payload>\n
//   \n
// Multiple `data:` lines per frame are joined with newlines; we only emit
// a single-line JSON per event so we don't bother handling multi-line.

import type { AgentEvent, AgentVariant } from "./types";

// Model choices shown in the header dropdown. Both backends accept any
// string in their `model` field and pass it straight to OpenAIModel; this
// list just constrains what the UI offers.
export const SUPPORTED_MODELS = [
  "gpt-5.4",
  "gpt-5.4-mini",
  "gpt-5",
  "gpt-5-mini",
  "gpt-4o-mini",
] as const;
export type SupportedModel = (typeof SUPPORTED_MODELS)[number];
export const DEFAULT_MODEL: SupportedModel = "gpt-5.4-mini";

export interface AgentService {
  variant: AgentVariant;
  baseUrl: string;
  label: string;       // e.g. "Customer Support · first-cut"
  caption: string;     // e.g. "cs-agent-first-cut"
}

export const AGENTS: Record<AgentVariant, AgentService> = {
  first_cut: {
    variant: "first_cut",
    baseUrl: "http://localhost:8001",
    label: "FIRST-CUT LOOP",
    caption: "cs-agent-first-cut · broad, implicit control",
  },
  engineered: {
    variant: "engineered",
    baseUrl: "http://localhost:8002",
    label: "ENGINEERED LOOP",
    caption: "cs-agent-engineered · explicit harness",
  },
};

export interface RunArgs {
  prompt: string;
  customer_id: string;
  model?: string;
  // engineered feature toggles. Ignored by first-cut.
  skills_enabled?: boolean;
  episodic_enabled?: boolean;
  // `planner_enabled` is per-request — no agent rebuild needed, so it can
  // flip mid-session unlike skills/episodic.
  planner_enabled?: boolean;
  run_id?: string;
  tool_budget?: number;
  signal?: AbortSignal;
  onEvent: (ev: AgentEvent) => void;
}

/** POST /api/run and stream SSE events through `onEvent`. */
export async function runAgent(svc: AgentService, args: RunArgs): Promise<void> {
  const response = await fetch(`${svc.baseUrl}/api/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      prompt: args.prompt,
      customer_id: args.customer_id,
      ...(args.model ? { model: args.model } : {}),
      ...(args.skills_enabled !== undefined
        ? { skills_enabled: args.skills_enabled }
        : {}),
      ...(args.episodic_enabled !== undefined
        ? { episodic_enabled: args.episodic_enabled }
        : {}),
      ...(args.planner_enabled !== undefined
        ? { planner_enabled: args.planner_enabled }
        : {}),
      ...(args.run_id ? { run_id: args.run_id } : {}),
      ...(args.tool_budget !== undefined ? { tool_budget: args.tool_budget } : {}),
    }),
    signal: args.signal,
  });

  if (!response.ok || !response.body) {
    throw new Error(`${svc.variant} /api/run returned ${response.status}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    // Normalize line endings as we go. The SSE spec allows \r\n, \r, or
    // \n; sse-starlette emits strict \r\n. Stripping \r lets us split on
    // \n\n regardless of which server we're talking to.
    buffer += decoder.decode(value, { stream: true }).replace(/\r/g, "");

    // Frames are separated by blank lines (\n\n). Process every complete
    // frame in the buffer; keep the partial tail for the next read.
    let sep = buffer.indexOf("\n\n");
    while (sep !== -1) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      sep = buffer.indexOf("\n\n");
      handleFrame(frame, args.onEvent);
    }
  }

  // Flush any remaining frame (partial reads at EOF).
  if (buffer.trim()) {
    handleFrame(buffer, args.onEvent);
  }
}

function handleFrame(frame: string, onEvent: (ev: AgentEvent) => void): void {
  let event = "message";
  const dataLines: string[] = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith(":")) continue; // SSE comment / keepalive ping
    if (line.startsWith("event:")) {
      event = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trim());
    }
  }
  if (!dataLines.length) return;
  const payload = dataLines.join("\n");
  let parsed: unknown;
  try {
    parsed = JSON.parse(payload);
  } catch {
    return; // Drop unparseable frames silently.
  }
  if (typeof parsed !== "object" || parsed === null) return;
  onEvent({ ...(parsed as object), type: event } as AgentEvent);
}

/** POST /api/reset. Fire-and-forget. */
export async function resetAgent(svc: AgentService): Promise<void> {
  const response = await fetch(`${svc.baseUrl}/api/reset`, { method: "POST" });
  if (!response.ok) {
    throw new Error(`${svc.variant} /api/reset returned ${response.status}`);
  }
}

/** POST /api/end_session — simulates time passing for the episodic-memory
 *  demo. Drops the cached Agent (clears conversation memory) but preserves
 *  the customer's episodic memory file on disk and the mock backend state. */
export async function endSession(svc: AgentService, customerId: string): Promise<void> {
  const response = await fetch(`${svc.baseUrl}/api/end_session`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ customer_id: customerId }),
  });
  if (!response.ok) {
    throw new Error(`${svc.variant} /api/end_session returned ${response.status}`);
  }
}

/** Episodic memory file contents for one customer. */
export interface AgentMemory {
  customer_id: string;
  exists: boolean;
  content: string;
}

/** GET /api/memory — the customer's episodic-memory file. engineered only; first-cut
 *  has no episodic memory. Returns `exists: false` with an empty
 *  `content` when no file has been written yet. */
export async function fetchMemory(
  svc: AgentService,
  customerId: string,
): Promise<AgentMemory> {
  const qs = `?customer_id=${encodeURIComponent(customerId)}`;
  const response = await fetch(`${svc.baseUrl}/api/memory${qs}`);
  if (!response.ok) {
    throw new Error(`${svc.variant} /api/memory returned ${response.status}`);
  }
  return (await response.json()) as AgentMemory;
}

/** One tool entry as returned by the agent's tool registry. */
export interface AgentTool {
  name: string;
  description?: string;
  inputSchema?: {
    json?: {
      properties?: Record<string, { type?: string; description?: string }>;
      required?: string[];
    };
  };
}

/** GET /api/tools — the tools this agent has registered, plus its system
 *  prompt. Both are properties of the agent, not of any one turn, so they
 *  are read here rather than picked out of a run's event stream.
 *  engineered honors `skills_enabled` / `episodic_enabled` query params so the
 *  drawer matches the live tool set for the current toggle state. first-cut
 *  ignores them. UI refetches whenever the toggles flip. */
export interface AgentCatalog {
  tools: AgentTool[];
  /** The agent's rendered system prompt for the current toggle combo. */
  systemPrompt: string;
}

export async function fetchTools(
  svc: AgentService,
  flags?: { skills_enabled?: boolean; episodic_enabled?: boolean },
): Promise<AgentCatalog> {
  const params = new URLSearchParams();
  if (flags?.skills_enabled !== undefined) {
    params.set("skills_enabled", String(flags.skills_enabled));
  }
  if (flags?.episodic_enabled !== undefined) {
    params.set("episodic_enabled", String(flags.episodic_enabled));
  }
  const qs = params.toString() ? `?${params}` : "";
  const response = await fetch(`${svc.baseUrl}/api/tools${qs}`);
  if (!response.ok) {
    throw new Error(`${svc.variant} /api/tools returned ${response.status}`);
  }
  const body = (await response.json()) as {
    tools?: AgentTool[];
    system_prompt?: string;
  };
  return { tools: body.tools ?? [], systemPrompt: body.system_prompt ?? "" };
}

/** One turn as the agent remembers it, rebuilt from `agent.messages`. */
export interface RestoredTurn {
  user_prompt: string;
  final_reply: string;
  trace: Array<{
    tool_use_id: string;
    name: string;
    args: unknown;
    args_summary: string;
    result?: unknown;
    result_summary?: string;
    is_error?: boolean;
  }>;
}

/** GET /api/session — the conversation currently in the agent's memory.
 *  Lets the console rebuild the thread after a browser refresh instead of
 *  showing an empty panel beside an agent that still remembers everything.
 *  engineered scopes the session by customer; first-cut has one shared agent. */
export async function fetchSession(
  svc: AgentService,
  customerId: string,
): Promise<RestoredTurn[]> {
  const qs =
    svc.variant === "engineered"
      ? `?customer_id=${encodeURIComponent(customerId)}`
      : "";
  const response = await fetch(`${svc.baseUrl}/api/session${qs}`);
  if (!response.ok) {
    throw new Error(`${svc.variant} /api/session returned ${response.status}`);
  }
  const body = (await response.json()) as { turns?: RestoredTurn[] };
  return body.turns ?? [];
}

export interface EvaluationSuite {
  results: Array<{
    scenario_id: string;
    case: string;
    kind: "outcome_and_trajectory";
    first_cut: { passed: boolean };
    engineered: { passed: boolean };
    expected_behavior: { first_cut: string; engineered: string };
    outcome_assertions: Array<{ name: string; passed: boolean }>;
    trajectory_assertions: Array<{ name: string; passed: boolean }>;
    evidence: Record<string, unknown>;
  }>;
  summary: { first_cut: number; engineered: number; total: number };
}

export async function runEvaluationSuite(): Promise<EvaluationSuite> {
  const response = await fetch(`${AGENTS.engineered.baseUrl}/api/evaluations/run`, { method: "POST" });
  if (!response.ok) throw new Error(`evaluation suite returned ${response.status}`);
  return (await response.json()) as EvaluationSuite;
}
