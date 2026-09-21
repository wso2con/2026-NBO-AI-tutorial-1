import { Fragment, useEffect, useRef, useState } from "react";
import {
  User,
  AlertCircle,
  AlertTriangle,
  CheckCircle2,
  HelpCircle,
  ChevronRight,
  Hourglass,
  ListChecks,
  ShieldAlert,
  X,
  XCircle,
} from "lucide-react";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type {
  EvaluationAspect,
  PauseInfo,
  PendingWrite,
  Turn,
  TurnEvaluation,
  TurnUsage,
} from "@/lib/types";
import { TraceRow } from "./TraceRow";
import { JsonView } from "./JsonView";
import { Markdown } from "./Markdown";
import { TextViewer } from "./TextViewer";

interface Props {
  turn: Turn;
  isLatest: boolean;
  /** Answers the turn's inline pause prompt. Omitted where turns are
   *  read-only (restored threads). `decisions` carries one answer per parked
   *  write (keyed by interrupt id) when the pause is a write confirmation;
   *  the budget pause has nothing to key and passes only `approved`. */
  onAnswerPause?: (
    turn: Turn,
    approved: boolean,
    decisions?: Record<string, boolean>,
  ) => void;
  pauseBusy?: boolean;
}

/**
 * One turn in the conversation thread: user prompt, the framed message the
 * agent received, ordered trace rows, and the agent's reply at the bottom.
 */
export function TurnCard({ turn, isLatest, onAnswerPause, pauseBusy }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);

  // Write decisions live in the turn's sequence, not in an epilogue to it.
  // Each one claims the slot under the trace row for the call it gates, and
  // the answered record replaces the question in that same slot — so nothing
  // shifts when the resumed turn streams further rows and a reply beneath it.
  const open =
    turn.pause?.code === "write_confirmation_required"
      ? (turn.pause.awaiting ?? [])
      : [];
  const answered = turn.approvals ?? [];
  // This card's unsent answers. A batch only goes back once every write in it
  // has one, so the first click has to show somewhere in the meantime.
  const [draft, setDraft] = useState<Record<string, boolean>>({});

  function decide(write: PendingWrite, approved: boolean) {
    if (pauseBusy || !onAnswerPause) return;
    const next = { ...draft, [write.id]: approved };
    setDraft(next);
    // The loop is parked on every queued write at once, so it only resumes
    // once each one has an answer. Sending early would either run writes the
    // customer has not seen or drop the ones they already approved.
    if (open.every((w) => next[w.id] !== undefined)) {
      onAnswerPause(
        turn,
        open.every((w) => next[w.id]),
        next,
      );
    }
  }

  const renderOpen = (write: PendingWrite) =>
    draft[write.id] === undefined ? (
      <WriteConsentCard
        key={write.id}
        write={write}
        pause={turn.pause!}
        busy={Boolean(pauseBusy) || !onAnswerPause}
        onDecide={decide}
      />
    ) : (
      <ApprovalRow key={write.id} write={write} approved={draft[write.id]} />
    );

  // Answered first, then anything still open: within one trace row that is
  // also the order the decisions were made in.
  const decisionsFor = (toolUseId: string) => (
    <>
      {answered
        .filter((a) => a.write.tool_use_id === toolUseId)
        .map((a) => (
          <ApprovalRow key={a.write.id} write={a.write} approved={a.approved} />
        ))}
      {open.filter((w) => w.tool_use_id === toolUseId).map(renderOpen)}
    </>
  );

  // A decision whose call is not in the trace (a restored thread, or a service
  // that predates `tool_use_id`) still has to be shown and still has to be
  // answerable, so it falls to the end of the trace rather than disappearing.
  const inTrace = (toolUseId: string) =>
    turn.trace.some((r) => r.tool_use_id === toolUseId);
  const strayAnswered = answered.filter((a) => !inTrace(a.write.tool_use_id));
  const strayOpen = open.filter((w) => !inTrace(w.tool_use_id));

  // Auto-scroll the newest turn into view while it's still streaming.
  useEffect(() => {
    if (isLatest && turn.status === "running") {
      containerRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
    }
  }, [turn.trace.length, turn.loop_events.length, turn.streaming_reply, isLatest, turn.status]);

  const reply = turn.final_reply || turn.streaming_reply;
  const framedDiffers = turn.framed_message && turn.framed_message !== turn.user_prompt;

  return (
    <div
      ref={containerRef}
      className="flex animate-fade-in flex-col gap-2 rounded-lg border bg-card p-3"
    >
      {/* User prompt bubble */}
      <div className="flex items-start gap-2">
        <div className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-primary text-primary-foreground">
          <User className="h-3.5 w-3.5" />
        </div>
        <div className="min-w-0 flex-1 whitespace-pre-wrap rounded-md bg-muted px-3 py-2 text-sm leading-relaxed">
          {turn.user_prompt}
          {turn.restored && (
            <span
              className="ml-2 align-middle text-[10px] uppercase tracking-wider text-muted-foreground"
              title="Rebuilt from the agent's session memory after a page refresh. Detail the message log doesn't keep, such as the framed message, is not shown."
            >
              · from memory
            </span>
          )}
        </div>
      </div>

      {/* Plan — engineered + planner-enabled only. Shown inline (not a drawer) so
          the audience can read the planner's intent / approach / skills /
          policies decision before any tool call streams in. */}
      {turn.plan && (
        <div className="ml-9 rounded-md border bg-muted px-3 py-2 text-xs">
          <div className="mb-1.5 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider text-engineered">
            <ListChecks className="h-3.5 w-3.5" />
            plan
          </div>
          <TextViewer text={turn.plan} allowRendered={false} maxHeight="max-h-72" />
        </div>
      )}

      {/* The message the agent actually received. Per-turn by nature — the
          framing, and on the first turn of a session the episodic-memory
          block, are prepended to what the user typed. The system prompt is
          not per-turn, so it lives in the panel drawer instead. */}
      {turn.framed_message && (
        <div className="flex flex-col gap-1 pl-9 text-xs">
          <Drawer
            label="message to agent"
            suffix={framedDiffers ? "(framed)" : undefined}
            charCount={turn.framed_message.length}
            content={turn.framed_message}
            maxHeight="max-h-48"
          />
        </div>
      )}

      {/* Trace rows, with each held write's consent control sitting directly
          under the call it gates. */}
      {(turn.trace.length > 0 || open.length > 0 || answered.length > 0) && (
        <div className="flex flex-col gap-1 pl-9">
          {turn.trace.map((row) => (
            <Fragment key={row.tool_use_id}>
              <TraceRow row={row} />
              {decisionsFor(row.tool_use_id)}
            </Fragment>
          ))}
          {strayAnswered.map((a) => (
            <ApprovalRow key={a.write.id} write={a.write} approved={a.approved} />
          ))}
          {strayOpen.map(renderOpen)}
        </div>
      )}

      {/* Error */}
      {turn.error && (
        <div
          className={cn(
            "ml-9 flex items-start gap-2 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive",
          )}
        >
          <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>{turn.error}</span>
        </div>
      )}

      {/* Reply */}
      {reply && (
        <div className="ml-9 rounded-md border bg-muted/40 px-3 py-2.5 text-sm">
          <div className="mb-1 flex items-center gap-1.5 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
            <span>reply</span>
            {turn.status === "running" && (
              <span className="inline-block h-1 w-1 animate-pulse rounded-full bg-muted-foreground" />
            )}
            {turn.evaluation && <ReviewChip evaluation={turn.evaluation} />}
          </div>
          <Markdown density="reply">{reply}</Markdown>
          {turn.usage && <UsageLine usage={turn.usage} />}
          {turn.usage && <ContextBars usage={turn.usage} />}
        </div>
      )}

      {/* The budget guard's question is about the run as a whole, not about
          one call, so it belongs under the wrap-up reply that explains it.
          Write confirmations render up in the trace, on their own call. */}
      {turn.pause?.code === "budget_grant_required" && onAnswerPause && (
        <BudgetPause turn={turn} onAnswer={onAnswerPause} busy={Boolean(pauseBusy)} />
      )}
    </div>
  );
}

/** Per-verdict presentation, in one place so the chip and the expanded rows
 *  cannot drift apart. `unavailable` is deliberately neutral: a reviewer that
 *  could not be reached says nothing about the agent's work, and must not
 *  read as either a pass or a defect. */
const REVIEW_STYLES = {
  pass: {
    Icon: CheckCircle2,
    tone: "text-emerald-700 dark:text-emerald-400",
    chip: "border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400",
    label: "reviewed",
  },
  warn: {
    Icon: AlertTriangle,
    tone: "text-amber-700 dark:text-amber-400",
    chip: "border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-400",
    label: "review: warning",
  },
  fail: {
    Icon: XCircle,
    tone: "text-destructive",
    chip: "border-destructive/40 bg-destructive/10 text-destructive",
    label: "review: issue",
  },
  unavailable: {
    Icon: HelpCircle,
    tone: "text-muted-foreground",
    chip: "border-border bg-muted/60 text-muted-foreground",
    label: "review unavailable",
  },
} as const;

/** The review chip beside the reply.
 *
 *  It is an annotation, never a gate. The reply above it was released before
 *  any of this ran, so while the reviews are in flight the chip shows a
 *  pending state rather than holding anything back. Clicking it expands one
 *  row per aspect with that reviewer's reasoning and cited evidence. */
function ReviewChip({ evaluation }: { evaluation: TurnEvaluation }) {
  const [open, setOpen] = useState(false);
  const pending = evaluation.status !== "complete";
  const style = REVIEW_STYLES[evaluation.verdict ?? "unavailable"];
  const done = evaluation.aspects.length;
  const total = Math.max(evaluation.expected.length, done);

  if (pending) {
    return (
      <span className="ml-auto inline-flex items-center gap-1 rounded-full border border-border bg-muted/60 px-2 py-0.5 text-[10px] font-medium normal-case tracking-normal text-muted-foreground">
        <Hourglass className="h-3 w-3 animate-pulse" />
        reviewing{total ? ` ${done}/${total}` : ""}
      </span>
    );
  }

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className={cn(
          "ml-auto inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-medium normal-case tracking-normal transition-colors hover:brightness-105",
          style.chip,
        )}
      >
        <style.Icon className="h-3 w-3" />
        {style.label}
        <ChevronRight className="h-3 w-3" />
      </button>
      {open && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/45 p-4"
          role="dialog"
          aria-modal="true"
          aria-label="LLM review details"
          onClick={() => setOpen(false)}
        >
          <div
            className="w-full max-w-2xl rounded-xl border bg-card p-4 shadow-2xl"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="mb-3 flex items-center gap-2">
              <style.Icon className={cn("h-4 w-4", style.tone)} />
              <div className="text-sm font-semibold">LLM review</div>
              <span className={cn("text-xs", style.tone)}>{evaluation.verdict}</span>
              <Button
                variant="ghost"
                size="icon"
                className="ml-auto h-7 w-7"
                onClick={() => setOpen(false)}
                aria-label="Close review"
              >
                <X className="h-4 w-4" />
              </Button>
            </div>
            <div className="flex max-h-[70vh] flex-col gap-2 overflow-y-auto">
              {evaluation.aspects.map((aspect) => (
                <ReviewAspectRow key={aspect.aspect} aspect={aspect} />
              ))}
            </div>
          </div>
        </div>
      )}
    </>
  );
}

function ReviewAspectRow({ aspect }: { aspect: EvaluationAspect }) {
  const style = REVIEW_STYLES[aspect.verdict] ?? REVIEW_STYLES.unavailable;
  return (
    <div className="rounded-md border bg-background/60 px-2.5 py-1.5 text-xs normal-case tracking-normal">
      <div className="flex items-center gap-1.5 font-medium">
        <style.Icon className={cn("h-3.5 w-3.5", style.tone)} />
        <span>{aspect.label}</span>
        <span className={cn("ml-auto text-[10px]", style.tone)}>{aspect.verdict}</span>
      </div>
      <div className="mt-1 font-normal text-muted-foreground">{aspect.summary}</div>
      {aspect.findings.length > 0 && (
        <ul className="mt-1.5 flex flex-col gap-1">
          {aspect.findings.map((finding, index) => (
            <li key={index} className="border-l-2 border-border pl-2 font-normal">
              <div className="text-foreground/80">{finding.description}</div>
              {finding.evidence && (
                <div className="mt-0.5 text-[10px] text-muted-foreground/80">
                  {finding.evidence}
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** The token guard stopped the loop mid-task and is asking whether to buy it
 *  another grant. One question, one answer. */
function BudgetPause({
  turn,
  onAnswer,
  busy,
}: {
  turn: Turn;
  onAnswer: (turn: Turn, approved: boolean) => void;
  busy: boolean;
}) {
  const pause = turn.pause!;
  const answered = turn.pause_answered;
  return (
    <div className="ml-9 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <Hourglass className="h-3.5 w-3.5 shrink-0 text-amber-600 dark:text-amber-500" />
        <span className="font-medium">{pause.question}</span>
        <span className="text-muted-foreground">
          {(pause.tokens_used ?? 0).toLocaleString()} of{" "}
          {(pause.token_ceiling ?? 0).toLocaleString()} total tokens used
          {Boolean(pause.grants_so_far) &&
            ` · ${pause.grants_so_far} extension${pause.grants_so_far === 1 ? "" : "s"} so far`}
        </span>
      </div>
      {answered ? (
        <div className="mt-1.5 text-muted-foreground">
          {answered === "confirmed"
            ? `Continued with ${(pause.grant_tokens ?? 0).toLocaleString()} more total tokens.`
            : "Stopped here. Nothing was changed."}
        </div>
      ) : (
        <div className="mt-2 flex gap-2">
          <Button
            size="sm"
            className="h-6 px-2 text-[11px]"
            disabled={busy}
            onClick={() => onAnswer(turn, true)}
          >
            {pause.confirm_label}
          </Button>
          <Button
            size="sm"
            variant="outline"
            className="h-6 px-2 text-[11px]"
            disabled={busy}
            onClick={() => onAnswer(turn, false)}
          >
            {pause.reject_label}
          </Button>
        </div>
      )}
    </div>
  );
}

/** One held write, asked as a question the customer can actually check.
 *
 *  It states the action in words and lists the exact values the call will use,
 *  all built by the harness from the same arguments the tool will receive, so
 *  the thing approved and the thing that runs cannot drift. A serialised
 *  function call would be the same information in a form nobody audits. */
function WriteConsentCard({
  write,
  pause,
  busy,
  onDecide,
}: {
  write: PendingWrite;
  pause: PauseInfo;
  busy: boolean;
  onDecide: (write: PendingWrite, approved: boolean) => void;
}) {
  return (
    <div className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2.5 text-xs">
      <div className="mb-1.5 flex items-center gap-1.5 text-[10px] font-medium uppercase tracking-wider text-amber-700 dark:text-amber-500">
        <ShieldAlert className="h-3.5 w-3.5 shrink-0" />
        <span>held for your approval</span>
      </div>
      <div className="text-sm font-medium">{write.title || write.question}</div>
      <WriteDetails write={write} />
      <div className="mt-2 flex gap-2">
        <Button
          size="sm"
          className="h-6 px-2 text-[11px]"
          disabled={busy}
          onClick={() => onDecide(write, true)}
        >
          {pause.confirm_label}
        </Button>
        <Button
          size="sm"
          variant="outline"
          className="h-6 px-2 text-[11px]"
          disabled={busy}
          onClick={() => onDecide(write, false)}
        >
          {pause.reject_label}
        </Button>
      </div>
    </div>
  );
}

/** The values the call will use, shown the same way whether the decision is
 *  still open or already made — so the expanded record is the card the
 *  customer actually answered, not a summary written afterwards. */
function WriteDetails({ write }: { write: PendingWrite }) {
  return (
    <>
      {write.fields && write.fields.length > 0 && (
        <dl className="mt-1.5 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
          {write.fields.map((field, i) => (
            <div key={i} className="contents">
              <dt className="text-muted-foreground">{field.label}</dt>
              <dd className="break-words font-medium">{field.value}</dd>
            </div>
          ))}
        </dl>
      )}
      {write.effect && (
        <div className="mt-1.5 text-muted-foreground">{write.effect}</div>
      )}
    </>
  );
}

/** One answered write, collapsed to a tool-row one-liner beneath the reply.
 *  Expanding it shows the card as it was put to the customer plus the raw
 *  arguments, so the record is auditable rather than merely reassuring. */
function ApprovalRow({
  write,
  approved,
}: {
  write: PendingWrite;
  approved: boolean;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div
      className={cn(
        "rounded-md border transition-colors",
        approved ? "border-border" : "border-border bg-muted/30",
      )}
    >
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="flex w-full items-center gap-2 rounded-md px-3 py-2 text-left text-xs font-mono hover:bg-accent"
      >
        <ChevronRight
          className={cn("h-3 w-3 shrink-0 transition-transform", open && "rotate-90")}
        />
        <ShieldAlert className="h-3.5 w-3.5 shrink-0 text-amber-600 dark:text-amber-500" />
        <span className="font-semibold">human approval</span>
        <span className="truncate text-muted-foreground">
          {write.title || write.tool}
        </span>
        <span className="mx-1 text-muted-foreground">→</span>
        <span
          className={cn(
            "shrink-0",
            approved ? "text-muted-foreground" : "text-foreground",
          )}
        >
          {approved ? "approved" : "declined"}
        </span>
        {approved ? (
          <CheckCircle2 className="ml-auto h-3.5 w-3.5 shrink-0 text-success" />
        ) : (
          <XCircle className="ml-auto h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        )}
      </button>
      {open && (
        <div className="flex flex-col gap-2 border-t bg-muted/40 px-3 py-2 text-xs">
          <div>
            <div className="mb-1 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
              what you were asked
            </div>
            <div className="font-medium">{write.title || write.question}</div>
            <WriteDetails write={write} />
          </div>
          <div>
            <div className="mb-1 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
              {approved ? `${write.tool} — ran with` : `${write.tool} — blocked, would have run with`}
            </div>
            <JsonView value={write.args} maxHeight="max-h-56" />
          </div>
        </div>
      )}
    </div>
  );
}

/** Cumulative loop usage for this chat session. */
function UsageLine({ usage }: { usage: TurnUsage }) {
  const overBudget = usage.budget_used_percent >= 90;
  return (
    <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 border-t pt-1.5 text-[10px] tabular-nums text-muted-foreground">
      <span title="Cumulative agent-loop input plus output for this chat session.">
        <span className={cn("font-medium", overBudget && "text-amber-600 dark:text-amber-500")}>
          {usage.loop_total_tokens.toLocaleString()}
        </span>
        {" / "}
        {usage.token_budget.toLocaleString()} total ({usage.budget_used_percent}%)
      </span>
      <span>
        {usage.loop_input_tokens.toLocaleString()} input ·{" "}
        {usage.loop_generated_tokens.toLocaleString()} output
      </span>
      <span>
        {usage.model_calls} model {usage.model_calls === 1 ? "call" : "calls"} this turn
      </span>
    </div>
  );
}

/** Exact provider-reported total input for every completed model call. The
 * fixed segment is anchored to the first exact call minus its small estimated
 * user message; later message/observation context is the exact remainder. */
function ContextBars({ usage }: { usage: TurnUsage }) {
  const calls = (usage.model_call_usage ?? [])
    .map((item) => ({
      call: item.call,
      tokens: item.input_tokens,
      fixed: Math.min(item.input_tokens, item.fixed_context_tokens ?? 0),
      dynamic: Math.max(
        0,
        item.dynamic_context_tokens ?? item.input_tokens - (item.fixed_context_tokens ?? 0),
      ),
    }))
    .filter((item) => item.tokens > 0);

  if (calls.length === 0) return null;
  const max = Math.max(...calls.map((item) => item.tokens), 1);

  return (
    <div className="mt-2 border-t pt-2 text-[10px] text-muted-foreground">
      <div className="mb-1 flex items-center justify-between">
        <span className="font-medium text-foreground/70">context sent per model call</span>
        <span>input tokens</span>
      </div>
      <div className="mb-1.5 flex flex-wrap gap-x-3 gap-y-1 text-[9px]">
        <span className="inline-flex items-center gap-1">
          <span className="h-2 w-2 rounded-sm bg-violet-500/75" />
          system + tools (first-call baseline)
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="h-2 w-2 rounded-sm bg-sky-500/75" />
          messages + observations
        </span>
      </div>
      <div className="flex flex-col gap-1">
        {calls.map((item) => (
          <div key={item.call} className="grid grid-cols-[34px_1fr_52px] items-center gap-2 tabular-nums">
            <span>call {item.call}</span>
            <div className="h-1.5 overflow-hidden rounded-full bg-muted">
              <div
                className="flex h-full min-w-[3px] overflow-hidden rounded-full"
                style={{ width: `${Math.max(2, (item.tokens / max) * 100)}%` }}
                title={`System + tools baseline: ${item.fixed.toLocaleString()} · Messages + observations: ${item.dynamic.toLocaleString()}`}
              >
                <div
                  className="h-full bg-violet-500/75"
                  style={{ width: `${item.tokens ? (item.fixed / item.tokens) * 100 : 0}%` }}
                />
                <div className="h-full flex-1 bg-sky-500/75" />
              </div>
            </div>
            <span className="text-right">{item.tokens.toLocaleString()}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

interface DrawerProps {
  label: string;
  suffix?: string;
  charCount: number;
  content: string;
  maxHeight: string;
}

function Drawer({ label, suffix, charCount, content, maxHeight }: DrawerProps) {
  return (
    <Collapsible className="rounded-md border bg-muted/40">
      <CollapsibleTrigger className="flex w-full items-center gap-2 px-2 py-1.5 hover:bg-accent group">
        <ChevronRight className="h-3 w-3 transition-transform group-data-[state=open]:rotate-90" />
        <span className="font-medium">{label}</span>
        {suffix && <span className="text-muted-foreground">{suffix}</span>}
        <span className="ml-auto text-muted-foreground">
          {charCount.toLocaleString()} chars
        </span>
      </CollapsibleTrigger>
      <CollapsibleContent className="overflow-hidden data-[state=open]:animate-accordion-down data-[state=closed]:animate-accordion-up">
        <div className="border-t p-1.5">
          {/* Raw by default: the drawer exists to show the literal bytes the
              model received. "rendered" is one click away for the audience
              that wants to read it as the document it was authored as. */}
          <TextViewer text={content} defaultView="raw" maxHeight={maxHeight} />
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}
