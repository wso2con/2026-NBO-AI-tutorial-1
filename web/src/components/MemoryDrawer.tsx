import { useCallback, useEffect, useState } from "react";
import { Brain, ChevronRight, RotateCw } from "lucide-react";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";
import { fetchMemory, type AgentMemory, type AgentService } from "@/lib/api";
import { TextViewer } from "./TextViewer";

interface Props {
  service: AgentService;
  customerId: string;
  /** Bumped by the parent after events that may have changed the file on
   *  disk (turn complete, reset, next session). Triggers a refetch so the
   *  drawer reflects what the agent just wrote. */
  refreshKey: number;
}

/**
 * Reads the engineered agent's episodic-memory file for the current customer and
 * renders it inline as markdown. Sits below the tools drawer in the engineered
 * panel and is only mounted when the episodic-memory feature toggle is on.
 *
 * This is the "look inside the file" affordance for the §5 demo: the
 * presenter flips memory on, runs a turn, ends the session, then expands
 * this drawer to show the audience what got committed to disk.
 */
export function MemoryDrawer({ service, customerId, refreshKey }: Props) {
  const [memory, setMemory] = useState<AgentMemory | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(() => {
    let cancelled = false;
    setLoading(true);
    fetchMemory(service, customerId)
      .then((m) => {
        if (cancelled) return;
        setMemory(m);
        setError(null);
      })
      .catch((err) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (cancelled) return;
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [service, customerId]);

  useEffect(() => refresh(), [refresh, refreshKey]);

  const exists = memory?.exists ?? false;
  const chars = memory?.content.length ?? 0;
  const status = loading
    ? "(loading...)"
    : error
      ? "(error)"
      : exists
        ? `(${chars.toLocaleString()} chars)`
        : "(empty)";

  return (
    <Collapsible className="border-b bg-card">
      <div className="flex w-full items-center text-xs">
        <CollapsibleTrigger className="group flex flex-1 items-center gap-2 px-4 py-2 text-left hover:bg-accent">
          <ChevronRight className="h-3 w-3 transition-transform group-data-[state=open]:rotate-90" />
          <Brain className="h-3 w-3 text-muted-foreground" />
          <span className="font-medium">episodic memory</span>
          <span className="text-muted-foreground">{status}</span>
          <span className="ml-auto truncate font-mono text-[10px] text-muted-foreground">
            {customerId}
          </span>
        </CollapsibleTrigger>
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            refresh();
          }}
          disabled={loading}
          title="Refetch the memory file"
          className="mx-1 rounded p-1 text-muted-foreground hover:bg-accent disabled:cursor-not-allowed disabled:opacity-50"
        >
          <RotateCw className={cn("h-3 w-3", loading && "animate-spin")} />
        </button>
      </div>
      <CollapsibleContent className="overflow-hidden data-[state=open]:animate-accordion-down data-[state=closed]:animate-accordion-up">
        <div className="border-t bg-muted/40 px-3 py-2.5 text-xs">
          {error ? (
            <div className="text-destructive">{error}</div>
          ) : !exists ? (
            <div className="text-muted-foreground">
              No memory recorded for this customer yet. The agent appends
              notes via the <span className="font-mono">append_memory</span>{" "}
              tool during a turn.
            </div>
          ) : (
            /* A file the agent wrote for a human reader: rendered first,
               with "raw" there for the audience that wants to see the
               markdown the next turn will actually be fed. */
            <TextViewer
              text={memory!.content}
              defaultView="rendered"
              maxHeight="max-h-72"
            />
          )}
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}
