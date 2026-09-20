import { useState } from "react";
import { ChevronRight, AlertCircle, CheckCircle2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { JsonView } from "./JsonView";
import type { TraceRow as TraceRowData } from "@/lib/types";

interface Props {
  row: TraceRowData;
}

/**
 * One tool call rendered as a compact one-liner that expands into a card
 * showing the full args + result JSON. Click anywhere on the header row
 * to toggle.
 */
export function TraceRow({ row }: Props) {
  const [open, setOpen] = useState(false);
  const hasResult = row.result !== undefined;
  const isError = Boolean(row.is_error);

  return (
    <div
      className={cn(
        "rounded-md border transition-colors",
        isError ? "border-destructive/40 bg-destructive/5" : "border-border",
      )}
    >
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs font-mono hover:bg-accent rounded-md"
      >
        <ChevronRight
          className={cn("h-3 w-3 shrink-0 transition-transform", open && "rotate-90")}
        />
        <span className="font-semibold">{row.name}</span>
        <span className="truncate text-muted-foreground">({row.args_summary})</span>
        {hasResult ? (
          <>
            <span className="mx-1 text-muted-foreground">→</span>
            <span
              className={cn(
                "truncate",
                isError ? "text-destructive" : "text-muted-foreground",
              )}
            >
              {row.result_summary}
            </span>
            {isError ? (
              <AlertCircle className="ml-auto h-3.5 w-3.5 shrink-0 text-destructive" />
            ) : (
              <CheckCircle2 className="ml-auto h-3.5 w-3.5 shrink-0 text-success" />
            )}
          </>
        ) : (
          <span className="ml-auto h-2 w-2 shrink-0 animate-pulse rounded-full bg-muted-foreground" />
        )}
      </button>

      {open && (
        <div className="flex flex-col gap-2 border-t bg-muted/40 px-3 py-2 text-xs">
          <div>
            <div className="mb-1 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
              args
            </div>
            <JsonView value={row.args} maxHeight="max-h-56" />
          </div>
          {hasResult && (
            <div>
              <div className="mb-1 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
                {isError ? "result (error)" : "result"}
              </div>
              <JsonView value={row.result} maxHeight="max-h-72" isError={isError} />
            </div>
          )}
        </div>
      )}

    </div>
  );
}
