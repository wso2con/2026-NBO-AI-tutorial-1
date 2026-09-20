import { useEffect, useRef } from "react";
import { User, AlertCircle, ChevronRight, ListChecks } from "lucide-react";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";
import type { Turn } from "@/lib/types";
import { TraceRow } from "./TraceRow";
import { Markdown } from "./Markdown";
import { TextViewer } from "./TextViewer";

interface Props {
  turn: Turn;
  isLatest: boolean;
}

/**
 * One turn in the conversation thread: user prompt, the framed message the
 * agent received, ordered trace rows, and the agent's reply at the bottom.
 */
export function TurnCard({ turn, isLatest }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);

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
              title="Rebuilt from the agent's session memory after a page refresh. Detail the message log doesn't keep — the framed message — is not shown."
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

      {/* Trace rows */}
      {turn.trace.length > 0 && (
        <div className="flex flex-col gap-1 pl-9">
          {turn.trace.map((row) => (
            <TraceRow key={row.tool_use_id} row={row} />
          ))}
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
          </div>
          <Markdown density="reply">{reply}</Markdown>
        </div>
      )}
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
