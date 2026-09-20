import { useEffect, useRef } from "react";
import ReactMarkdown from "react-markdown";
import remarkBreaks from "remark-breaks";
import { User, AlertCircle, ChevronRight, ListChecks, Activity } from "lucide-react";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";
import type { Turn } from "@/lib/types";
import { TraceRow } from "./TraceRow";

interface Props {
  turn: Turn;
  isLatest: boolean;
}

/**
 * One turn in the conversation thread: user prompt, optional drawers for
 * the framed message + system prompt the agent received, ordered trace
 * rows, and the agent's reply at the bottom.
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
        </div>
      </div>

      {/* Plan — v2 + planner-enabled only. Shown inline (not a drawer) so
          the audience can read the planner's intent / approach / skills /
          policies decision before any tool call streams in. */}
      {turn.plan && (
        <div className="ml-9 rounded-md border bg-muted px-3 py-2 text-xs">
          <div className="mb-1.5 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider text-v2">
            <ListChecks className="h-3.5 w-3.5" />
            plan
          </div>
          <pre className="whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed text-foreground/90">
            {turn.plan}
          </pre>
        </div>
      )}

      {turn.loop_events.length > 0 && (
        <div className="ml-9 overflow-hidden rounded-md border bg-muted/30 text-xs">
          <div className="flex items-center gap-1.5 border-b px-3 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
            <Activity className="h-3.5 w-3.5" />
            loop trajectory
          </div>
          <div className="divide-y">
            {turn.loop_events.map((event, index) => (
              <Collapsible key={`${event.type}-${index}`}>
                <CollapsibleTrigger className="group flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-accent">
                  <ChevronRight className="h-3 w-3 shrink-0 transition-transform group-data-[state=open]:rotate-90" />
                  <span className="w-32 shrink-0 font-mono text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
                    {event.type.replaceAll("_", " ")}
                  </span>
                  <span className="min-w-0 flex-1 truncate">{event.summary}</span>
                </CollapsibleTrigger>
                <CollapsibleContent>
                  <pre className="max-h-64 overflow-auto whitespace-pre-wrap border-t bg-background px-3 py-2 font-mono text-[10px] leading-relaxed">
                    {JSON.stringify(event, null, 2)}
                  </pre>
                </CollapsibleContent>
              </Collapsible>
            ))}
          </div>
        </div>
      )}

      {/* Drawers: framed message + system prompt */}
      <div className="flex flex-col gap-1 pl-9 text-xs">
        {turn.framed_message && (
          <Drawer
            label="message to agent"
            suffix={framedDiffers ? "(framed)" : undefined}
            charCount={turn.framed_message.length}
            content={turn.framed_message}
            maxHeight="max-h-48"
          />
        )}
        {turn.system_prompt && (
          <Drawer
            label="system prompt"
            charCount={turn.system_prompt.length}
            content={turn.system_prompt}
            maxHeight="max-h-80"
          />
        )}
      </div>

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
          <div className="prose prose-sm max-w-none leading-relaxed dark:prose-invert prose-p:my-2 prose-headings:my-2 prose-ul:my-2 prose-ol:my-2 prose-li:my-0.5 prose-pre:my-2">
            <ReactMarkdown remarkPlugins={[remarkBreaks]}>{reply}</ReactMarkdown>
          </div>
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
        <pre className={cn("overflow-auto whitespace-pre-wrap border-t px-2 py-1.5 font-mono", maxHeight)}>
          {content}
        </pre>
      </CollapsibleContent>
    </Collapsible>
  );
}
