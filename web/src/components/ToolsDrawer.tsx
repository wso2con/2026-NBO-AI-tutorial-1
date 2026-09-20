import { useState } from "react";
import { ChevronRight, Wrench } from "lucide-react";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";
import type { AgentTool } from "@/lib/api";

interface Props {
  tools: AgentTool[];
  /** Show a "(loading)" state while the initial fetch is in flight. */
  loading?: boolean;
  /** Error from the fetch, if any. */
  error?: string;
}

/**
 * Lists the tools this agent has registered. Single first-level
 * collapsible at the panel level; each tool inside is further
 * click-expandable to show its full description and input schema.
 *
 * The v1↔v2 contrast lands here: v1 has 14 tools with one-line
 * docstrings; v2 has 13 typed MCP specs with rich descriptions.
 */
export function ToolsDrawer({ tools, loading, error }: Props) {
  return (
    <Collapsible className="border-y bg-card">
      <CollapsibleTrigger className="flex w-full items-center gap-2 px-4 py-2 text-xs hover:bg-accent group">
        <ChevronRight className="h-3 w-3 transition-transform group-data-[state=open]:rotate-90" />
        <Wrench className="h-3 w-3 text-muted-foreground" />
        <span className="font-medium">tools</span>
        <span className="text-muted-foreground">
          {loading
            ? "(loading…)"
            : error
              ? "(error)"
              : `(${tools.length})`}
        </span>
      </CollapsibleTrigger>
      <CollapsibleContent className="overflow-hidden data-[state=open]:animate-accordion-down data-[state=closed]:animate-accordion-up">
        <div className="border-t bg-muted/40">
          {error ? (
            <div className="px-4 py-3 text-xs text-destructive">{error}</div>
          ) : tools.length === 0 && !loading ? (
            <div className="px-4 py-3 text-xs text-muted-foreground">
              No tools registered.
            </div>
          ) : (
            <ul className="flex flex-col">
              {[...tools]
                .sort((a, b) => a.name.localeCompare(b.name))
                .map((t) => (
                  <ToolRow key={t.name} tool={t} />
                ))}
            </ul>
          )}
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}

function ToolRow({ tool }: { tool: AgentTool }) {
  const [open, setOpen] = useState(false);
  const description = (tool.description ?? "").trim();
  const firstLine = description.split("\n")[0] || "no description";
  const params = tool.inputSchema?.json?.properties ?? {};
  const required = new Set(tool.inputSchema?.json?.required ?? []);
  const paramNames = Object.keys(params);

  return (
    <li className="border-b last:border-b-0">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-start gap-2 px-4 py-2 text-left text-xs hover:bg-accent"
      >
        <ChevronRight
          className={cn(
            "mt-0.5 h-3 w-3 shrink-0 transition-transform",
            open && "rotate-90",
          )}
        />
        <span className="font-mono font-semibold">{tool.name}</span>
        <span className="truncate text-muted-foreground">{firstLine}</span>
      </button>
      {open && (
        <div className="border-t bg-background px-4 py-2.5 text-xs">
          {description && (
            <pre className="mb-2 whitespace-pre-wrap font-sans leading-relaxed text-foreground">
              {description}
            </pre>
          )}
          {paramNames.length > 0 ? (
            <>
              <div className="mb-1 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
                parameters
              </div>
              <ul className="flex flex-col gap-0.5 font-mono">
                {paramNames.map((p) => {
                  const meta = params[p];
                  return (
                    <li key={p} className="flex items-baseline gap-2">
                      <span className="font-semibold">{p}</span>
                      <span className="text-muted-foreground">
                        {meta?.type ?? "any"}
                      </span>
                      {required.has(p) && (
                        <span className="rounded bg-destructive/10 px-1 text-[10px] text-destructive">
                          required
                        </span>
                      )}
                      {meta?.description && (
                        <span className="text-muted-foreground">
                          · {meta.description}
                        </span>
                      )}
                    </li>
                  );
                })}
              </ul>
            </>
          ) : (
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
              no parameters
            </div>
          )}
        </div>
      )}
    </li>
  );
}
