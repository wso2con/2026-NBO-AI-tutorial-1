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

/** Token spend for the current chat session, reported alongside each reply.
 *  The budget meters loop input + output. Harness-side planner/reviewer calls
 *  remain diagnostic fields and are intentionally absent from the compact UI.
 *
 *  `loop_*` is the agent loop itself; `auxiliary_*` is the harness's own model
 *  calls (planner, policy validator, budget wrap-up), real spend that sits
 *  outside the loop's budget either way. */
export interface TurnUsage {
  /** Metered total: loop input plus loop output. */
  loop_total_tokens: number;
  loop_generated_tokens: number;
  auxiliary_generated_tokens: number;
  total_generated_tokens: number;
  loop_input_tokens: number;
  auxiliary_input_tokens: number;
  total_input_tokens: number;
  /** Largest single call's input: a soft high-water marker, not a limit. */
  peak_call_input_tokens: number;
  /** The session's current total-token ceiling: base budget plus grants. */
  token_budget: number;
  base_token_budget?: number;
  budget_grants?: number;
  /** Loop input + output against the ceiling. */
  budget_used_percent: number;
  model_calls: number;
  model_call_usage: Array<{
    call: number;
    input_tokens: number;
    output_tokens: number;
    total_tokens: number;
    fixed_context_tokens: number;
    dynamic_context_tokens: number;
  }>;
  tool_calls: number;
  /** engineered only — the auto-compaction line in force for this session, in
   *  projected input tokens for the next model call. Null / absent means the
   *  dial was off and context was left to grow. */
  compact_at_tokens?: number | null;
  /** engineered only — one entry per compaction the harness performed this
   *  turn, keyed to the model call it protected. */
  compactions?: Array<{
    call: number;
    threshold_tokens: number;
    before_tokens: number;
    after_tokens: number;
    saved_tokens: number;
    messages_before: number;
    messages_after: number;
    messages_summarized: number;
    overflow: boolean;
  }>;
}

/** A turn that ended holding work it will not do without a human decision.
 *  Both kinds travel in the same shape, so the console renders one inline
 *  control and no answer is ever parsed out of what the customer typed:
 *
 *  - `budget_grant_required` — the token guard paused the loop mid-task.
 *  - `write_confirmation_required` — a write tool is queued behind approval. */
/** One write the harness is holding, described by the harness rather than by
 *  the model: `title`/`fields`/`effect` are built from the same arguments the
 *  tool will receive, so the customer approves the call that actually runs.
 *  `id` is the interrupt id, and it is how a decision is sent back — parallel
 *  writes park separately and are answered separately. */
export interface PendingWrite {
  id: string;
  tool: string;
  /** The `toolUseId` of the call this decision gates, so the console can put
   *  the consent control on that trace row instead of at the end of the turn. */
  tool_use_id: string;
  args: Record<string, unknown>;
  question: string;
  title?: string;
  fields?: { label: string; value: string }[];
  effect?: string;
}

/** A write decision that has been made. The write travels with the answer so
 *  the record shows what was put to the customer, not a summary of it. */
export interface AnsweredWrite {
  write: PendingWrite;
  approved: boolean;
}

export interface PauseInfo {
  code: "budget_grant_required" | "write_confirmation_required";
  run_id: string;
  question: string;
  confirm_label: string;
  reject_label: string;
  options: string[];
  /** budget_grant_required only. Both figures are total loop tokens. */
  grant_tokens?: number;
  tokens_used?: number;
  token_ceiling?: number;
  grants_so_far?: number;
  /** write_confirmation_required only: the writes held at the harness, one
   *  entry per parked call and one consent control each. */
  awaiting?: PendingWrite[];
}

export interface DoneEvent {
  type: "done";
  final_reply: string;
  usage?: TurnUsage;
  pause?: PauseInfo;
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
  // engineered only — the harness paused a write tool and asked the customer
  // to confirm it, then recorded their yes / no on the next message.
  | "human_approval"
  // engineered only — the context pipeline summarized the oldest messages
  // before a model call because projected context crossed the operator's line.
  | "context_compacted"
  | "state_transition"
  | "loop_decision"
  | "recovery"
  | "reconciliation"
  | "completion"
  // Both agents — the post-turn review. These arrive AFTER `done`, so the
  // reply is already on screen by the time the first one lands. Nothing here
  // gates: enforcement happens at the tool boundary, before the action.
  | "evaluation_started"
  | "evaluation_aspect"
  | "evaluation_complete";

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
/** One reviewer's opinion of a finished turn. `unavailable` means that
 *  reviewer could not be reached, which is neither a pass nor a defect in the
 *  agent's work. */
export interface EvaluationAspect {
  aspect: string;
  label: string;
  verdict: "pass" | "warn" | "fail" | "unavailable";
  passed: boolean | null;
  summary: string;
  findings: { description?: string; evidence?: string }[];
}

/** The review as a whole, filled in progressively: `pending` from the moment
 *  the reviews are dispatched, then one aspect at a time as each returns. */
export interface TurnEvaluation {
  status: "pending" | "complete";
  verdict?: "pass" | "warn" | "fail" | "unavailable";
  expected: { aspect: string; label: string }[];
  aspects: EvaluationAspect[];
}

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
  usage?: TurnUsage;
  // Arrives after the reply, never with it. Undefined on a turn that was not
  // reviewed (a paused turn or restored turn).
  evaluation?: TurnEvaluation;
  pause?: PauseInfo;
  /** Set once the budget pause has been answered, so it stops asking. */
  pause_answered?: "confirmed" | "rejected";
  /** Every write decision made on this turn, in the order they were made.
   *  Kept separately from `pause` because `pause` only ever holds the question
   *  currently open: a turn that resumes and then queues another write gets a
   *  fresh `pause`, and the record of what was already authorised has to
   *  survive that. */
  approvals?: AnsweredWrite[];
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
