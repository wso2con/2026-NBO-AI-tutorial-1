import { useState } from "react";
import { Activity, ChevronRight } from "lucide-react";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";
import { JsonView } from "./JsonView";
import type { LoopEvent, LoopEventType } from "@/lib/types";

/**
 * The loop trajectory: what the harness decided, in the order it decided it.
 *
 * The stream is deliberately verbose — every tool result emits a full state
 * dump, a context-update echo and a CONTINUE decision. All of that is real
 * and stays in the stream, but showing it row-for-row buries the four or
 * five moments the demo is actually about (memory selected, skill loaded,
 * contract registered, release gate passed or failed, exit reason).
 *
 * So this view does three things:
 *   1. Folds the repeated state dumps into one live status strip.
 *   2. Shows only the events that change the story, each with its decisive
 *      field lifted onto the row so you can read it without expanding.
 *   3. Keeps "all N events" one click away — nothing is hidden, just quiet.
 */

interface Props {
  events: LoopEvent[];
}

export function LoopTrajectory({ events }: Props) {
  const [showAll, setShowAll] = useState(false);

  const signal = events.filter(isSignal);
  const shown = showAll ? events : signal;
  const muted = events.length - signal.length;
  const state = latestState(events);

  return (
    <div className="ml-9 overflow-hidden rounded-md border bg-muted/30 text-xs">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b px-3 py-1.5">
        <span className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
          <Activity className="h-3.5 w-3.5" />
          loop trajectory
        </span>
        {state && <StatusStrip state={state} />}
      </div>

      <div className="divide-y">
        {shown.map((event, index) => (
          <EventRow key={`${event.type}-${index}`} event={event} />
        ))}
      </div>

      {muted > 0 && (
        <button
          type="button"
          onClick={() => setShowAll((s) => !s)}
          className="w-full border-t px-3 py-1.5 text-left text-[10px] text-muted-foreground hover:bg-accent hover:text-foreground"
        >
          {showAll
            ? `hide ${muted} bookkeeping events`
            : `+ ${muted} bookkeeping events (state dumps, context echoes, CONTINUE)`}
        </button>
      )}
    </div>
  );
}

function EventRow({ event }: { event: LoopEvent }) {
  const mark = highlight(event);

  return (
    <Collapsible>
      <CollapsibleTrigger className="group flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-accent">
        <ChevronRight className="h-3 w-3 shrink-0 transition-transform group-data-[state=open]:rotate-90" />
        <span
          className={cn(
            "w-28 shrink-0 font-mono text-[10px] font-semibold uppercase tracking-wide",
            toneClass(mark?.tone) ?? "text-muted-foreground",
          )}
        >
          {event.type.replaceAll("_", " ")}
        </span>
        {mark ? (
          <span
            className={cn(
              "min-w-0 shrink-0 truncate font-mono text-[11px] font-semibold",
              toneClass(mark.tone) ?? "text-foreground",
            )}
          >
            {mark.text}
          </span>
        ) : null}
        <span className="min-w-0 flex-1 truncate text-muted-foreground">
          {event.summary}
        </span>
      </CollapsibleTrigger>
      <CollapsibleContent>
        <div className="border-t bg-background px-3 py-2">
          <JsonView value={detailOf(event)} defaultExpandDepth={1} maxHeight="max-h-64" />
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}

/** Budget + criteria, folded out of the repeated state dumps. */
function StatusStrip({ state }: { state: RunStateLike }) {
  const verified = Object.values(state.verified_criteria ?? {}).filter(Boolean).length;
  const required = state.success_criteria?.length ?? 0;
  const used = state.tool_call_count ?? 0;
  const budget = state.max_tool_calls ?? 0;
  const tight = budget > 0 && used / budget >= 0.75;

  return (
    <div className="ml-auto flex items-center gap-3 font-mono text-[10px]">
      {required > 0 && (
        <span
          className={cn(
            verified >= required ? "text-success" : "text-muted-foreground",
          )}
          title="Verified success criteria / required by the active contract"
        >
          criteria {verified}/{required}
        </span>
      )}
      {budget > 0 && (
        <span
          className={cn(tight ? "text-first-cut" : "text-muted-foreground")}
          title="Tool calls used / budget"
        >
          tools {used}/{budget}
        </span>
      )}
      {state.next_loop_state && (
        <span className="rounded bg-muted px-1.5 py-0.5 font-semibold">
          {state.next_loop_state}
        </span>
      )}
    </div>
  );
}

/* ---------- which events earn a row ---------- */

/**
 * Suppressed by default. Each is genuinely emitted, and each is either a
 * duplicate of something already on screen or a per-tool-call constant:
 *   state_transition — the full run state, folded into the status strip
 *   context_update   — the tool observation, already in the trace row above
 *   llm_decision     — "invoke <tool>", already in the trace row above
 */
const BOOKKEEPING: ReadonlySet<LoopEventType> = new Set([
  "state_transition",
  "context_update",
  "llm_decision",
]);

function isSignal(event: LoopEvent): boolean {
  if (BOOKKEEPING.has(event.type)) return false;
  // CONTINUE fires after every single observation; the decisions worth
  // reading are the ones that end or redirect the run.
  if (event.type === "loop_decision" && event.decision === "CONTINUE") return false;
  return true;
}

/* ---------- the one field that matters, per event type ---------- */

type Tone = "success" | "danger" | "warn" | undefined;

function toneClass(tone: Tone): string | undefined {
  if (tone === "success") return "text-success";
  if (tone === "danger") return "text-destructive";
  if (tone === "warn") return "text-first-cut";
  return undefined;
}

function highlight(event: LoopEvent): { text: string; tone: Tone } | null {
  switch (event.type) {
    case "context_build": {
      const ctx = event.context as Record<string, unknown> | undefined;
      if (!ctx) return null;
      const bits: string[] = [];
      const tools = ctx.available_tools;
      if (Array.isArray(tools)) bits.push(`${tools.length} tools`);
      const msgs = Number(ctx.conversation_messages ?? 0);
      bits.push(msgs > 0 ? `${msgs} prior msgs` : "fresh session");
      if (ctx.plan_present) bits.push("plan");
      const mem = ctx.memory as Record<string, unknown> | undefined;
      if (mem?.selected_into_this_context) bits.push("memory");
      return { text: bits.join(" · "), tone: undefined };
    }
    case "memory_retrieval": {
      const chars = String(event.content ?? "").length;
      return { text: `${chars.toLocaleString()} chars`, tone: undefined };
    }
    case "skill_load":
      return event.skill_name
        ? { text: String(event.skill_name), tone: undefined }
        : null;
    case "task_contract": {
      const contract = event.contract as Record<string, unknown> | undefined;
      const criteria = contract?.required_criteria;
      if (!Array.isArray(criteria)) return null;
      return { text: `+${criteria.length} criteria`, tone: undefined };
    }
    case "validation": {
      const v = event.validation as Record<string, unknown> | undefined;
      if (!v) return null;
      if (v.passed) return { text: "passed", tone: "success" };
      const issues = [
        ...asStrings(v.missing_criteria).map((m) => `missing ${m}`),
        ...asStrings(v.violations),
      ];
      return {
        text: issues.join(", ") || "failed",
        tone: v.violations && asStrings(v.violations).length ? "danger" : "warn",
      };
    }
    case "loop_decision": {
      const decision = String(event.decision ?? "");
      if (!decision) return null;
      return { text: decision, tone: decisionTone(decision) };
    }
    case "completion": {
      const reason = String(event.exit_reason ?? "");
      if (!reason) return null;
      return {
        text: reason,
        tone: reason === "VALIDATED_COMPLETE" ? "success" : "warn",
      };
    }
    default:
      return null;
  }
}

function decisionTone(decision: string): Tone {
  if (decision === "COMPLETE") return "success";
  if (decision === "ESCALATE" || decision === "STOP") return "danger";
  if (decision === "PAUSE" || decision === "BUDGET_EXHAUSTED") return "warn";
  return undefined;
}

/**
 * What the expanded row shows. `run_id` and `summary` are already on the row
 * or in the panel header; the full state dump is in the status strip. Drop
 * them so the expansion is the event's own payload, not a wall of context.
 */
function detailOf(event: LoopEvent): Record<string, unknown> {
  const { run_id, summary, type, ...rest } = event;
  void run_id;
  void summary;
  void type;
  return Object.keys(rest).length ? rest : { summary };
}

/* ---------- run state, folded out of the stream ---------- */

interface RunStateLike {
  verified_criteria?: Record<string, boolean>;
  success_criteria?: string[];
  tool_call_count?: number;
  max_tool_calls?: number;
  next_loop_state?: string;
}

/** The most recent event carrying a `state` dump wins — it is the newest truth. */
function latestState(events: LoopEvent[]): RunStateLike | null {
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const state = events[i].state;
    if (state && typeof state === "object") return state as RunStateLike;
  }
  return null;
}

function asStrings(value: unknown): string[] {
  return Array.isArray(value) ? value.map(String) : [];
}
