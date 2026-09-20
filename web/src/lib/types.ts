// Event shapes match what cs_agent_first_cut/main.py and cs_agent_engineered/main.py emit
// as SSE frames. Keep these in sync with the Python `_run_agent_stream`
// generators on both services.

export type AgentVariant = "first_cut" | "engineered";

export interface SystemPromptEvent {
  type: "system_prompt";
  content: string;
}

export interface UserMessageEvent {
  type: "user_message";
  content: string;
}

export interface PlanEvent {
  // engineered only — emitted right after /api/run starts if the planner toggle
  // is on. The content is the planner's <plan>…</plan> block, ready to
  // render verbatim. Frontend without a handler drops this silently.
  type: "plan";
  content: string;
}

export interface ToolCallEvent {
  type: "tool_call";
  tool_use_id: string;
  name: string;
  args: unknown;
  args_summary: string;
}

export interface ToolResultEvent {
  type: "tool_result";
  tool_use_id: string;
  result: unknown;
  result_summary: string;
  is_error: boolean;
}

export interface TextDeltaEvent {
  type: "text_delta";
  delta: string;
}

export interface DoneEvent {
  type: "done";
  final_reply: string;
}

export interface ErrorEvent {
  type: "error";
  message: string;
}

export type LoopEventType =
  | "context_build"
  | "context_iteration"
  | "context_update"
  | "memory_retrieval"
  | "memory_write"
  | "skill_load"
  | "task_contract"
  | "llm_decision"
  | "action_selection"
  | "state_transition"
  | "loop_decision"
  | "recovery"
  | "reconciliation"
  | "validation"
  | "completion"
  | "evaluation";

export interface LoopEvent {
  type: LoopEventType;
  run_id: string;
  summary: string;
  [key: string]: unknown;
}

export type AgentEvent =
  | SystemPromptEvent
  | UserMessageEvent
  | PlanEvent
  | ToolCallEvent
  | ToolResultEvent
  | TextDeltaEvent
  | DoneEvent
  | ErrorEvent
  | LoopEvent;

// One row in the trace UI. We pair each tool_call with its matching
// tool_result (if it has arrived) so they collapse to one expandable row.
export interface TraceRow {
  tool_use_id: string;
  name: string;
  args: unknown;
  args_summary: string;
  result?: unknown;
  result_summary?: string;
  is_error?: boolean;
}

// One full turn of conversation, from the user's prompt through the
// agent's final reply. We keep an array of these per agent so the chat
// view shows the whole session until the user hits reset.
export interface Turn {
  id: string;                // unique key for React rendering
  user_prompt: string;       // what the user typed
  framed_message: string;    // what the server reported handing to the LLM
  system_prompt: string;     // the rendered system prompt for this turn
  // engineered + planner-enabled only: the planner's <plan>…</plan> block, which
  // also lives inside `framed_message`. Surfaced as a separate field so
  // the trace UI can render it as its own visible step.
  plan?: string;
  run_id?: string;
  loop_events: LoopEvent[];
  trace: TraceRow[];
  streaming_reply: string;
  final_reply: string;
  status: "running" | "done" | "error";
  error: string | null;
  // When the user clicks "Next session" after this turn, this flag is set
  // and the panel renders a "─── new session ───" divider beneath it.
  session_ended_after?: boolean;
  // Rebuilt from the agent's message log after a page refresh, rather than
  // streamed live. The card marks these so the missing trace detail on an
  // older turn doesn't read as the agent having skipped the work.
  restored?: boolean;
}

export function newTurn(user_prompt: string): Turn {
  return {
    id:
      typeof crypto !== "undefined" && "randomUUID" in crypto
        ? crypto.randomUUID()
        : `${Date.now()}-${Math.random().toString(36).slice(2, 9)}`,
    user_prompt,
    framed_message: "",
    system_prompt: "",
    trace: [],
    loop_events: [],
    streaming_reply: "",
    final_reply: "",
    status: "running",
    error: null,
  };
}

/** Build a completed Turn from what the agent remembers, for thread restore.
 *  Restored turns have no system prompt or loop events — those were never in
 *  the message log — but the tool trace is recovered where it survived. */
export function restoredTurn(data: {
  user_prompt: string;
  final_reply: string;
  trace: TraceRow[];
}): Turn {
  return {
    ...newTurn(data.user_prompt),
    trace: data.trace,
    final_reply: data.final_reply,
    status: "done",
    restored: true,
  };
}

// One column's accumulated state across the whole session.
export interface AgentState {
  enabled: boolean;          // user toggle — when false, /api/run is skipped
  turns: Turn[];             // appended on each send; cleared on reset
}

export function emptyAgentState(): AgentState {
  return { enabled: true, turns: [] };
}
