import { ChevronRight, FileText } from "lucide-react";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { TextViewer } from "./TextViewer";

interface Props {
  content: string;
  loading?: boolean;
  error?: string;
}

/**
 * The agent's standing system prompt, from GET /api/tools.
 *
 * It sits beside the tools drawer because it is the same kind of thing: a
 * property of the agent, fixed for a given toggle combo, not something a
 * turn produces. Reading it from the catalog means it is on screen before
 * the first message is sent, and it refetches with the tool list whenever
 * the feature toggles flip.
 */
export function SystemPromptDrawer({ content, loading, error }: Props) {
  const status = loading
    ? "(loading…)"
    : error
      ? "(error)"
      : `(${content.length.toLocaleString()} chars)`;

  return (
    <Collapsible className="border-b bg-card">
      <CollapsibleTrigger className="group flex w-full items-center gap-2 px-4 py-2 text-xs hover:bg-accent">
        <ChevronRight className="h-3 w-3 transition-transform group-data-[state=open]:rotate-90" />
        <FileText className="h-3 w-3 text-muted-foreground" />
        <span className="font-medium">system prompt</span>
        <span className="text-muted-foreground">{status}</span>
      </CollapsibleTrigger>
      <CollapsibleContent className="overflow-hidden data-[state=open]:animate-accordion-down data-[state=closed]:animate-accordion-up">
        <div className="border-t bg-muted/40 px-3 py-2.5 text-xs">
          {error ? (
            <div className="text-destructive">{error}</div>
          ) : content ? (
            // Opens rendered: the prompt is authored as markdown in
            // agent-profile.yaml and reads as a document. "raw" is one click
            // away for showing the literal bytes the model receives.
            <TextViewer text={content} defaultView="rendered" maxHeight="max-h-80" />
          ) : (
            // /api/tools answered without a system_prompt field. That means the
            // service is running code from before it served one — the process
            // needs restarting, not the agent fixing.
            <div className="text-muted-foreground">
              This agent service didn't report a system prompt. It's running an
              older build, so restart it with{" "}
              <span className="font-mono">make dev</span>.
            </div>
          )}
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}
