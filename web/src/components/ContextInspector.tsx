import { ChevronRight } from "lucide-react";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";
import { JsonView } from "./JsonView";
import { TextViewer } from "./TextViewer";
import type { LoopEvent } from "@/lib/types";

/**
 * The actual context handed to one model call, opened up.
 *
 * `context_build` reports "7 prior messages" as a count because it runs
 * before the call and deliberately claims nothing it can't prove. The
 * messages themselves arrive on the `context_iteration` event, captured by
 * ContextTraceHook at BeforeModelCall — the real array, in order, with tool
 * calls and tool results inline. This renders that array as the conversation
 * it is, so "7 messages" is something you can open instead of take on faith.
 */

interface Props {
  event: LoopEvent;
}

export function ContextInspector({ event }: Props) {
  const messages = Array.isArray(event.messages) ? (event.messages as Message[]) : [];
  const systemPrompt = String(event.system_prompt ?? "");
  const contracts = event.tool_contracts;
  const contractCount = Array.isArray(contracts) ? contracts.length : 0;

  return (
    <div className="flex flex-col gap-1.5">
      <Section
        label="messages"
        count={`${messages.length}`}
        defaultOpen
      >
        {messages.length === 0 ? (
          <div className="px-2 py-1.5 text-[11px] text-muted-foreground">
            First call of the session — nothing but the system prompt and this
            request.
          </div>
        ) : (
          <div className="divide-y rounded-md border">
            {messages.map((m, i) => (
              <MessageRow key={i} index={i} message={m} />
            ))}
          </div>
        )}
      </Section>

      {systemPrompt && (
        <Section label="system prompt" count={`${systemPrompt.length.toLocaleString()} chars`}>
          <TextViewer text={systemPrompt} defaultView="raw" maxHeight="max-h-64" />
        </Section>
      )}

      {contractCount > 0 && (
        <Section label="tool contracts" count={`${contractCount}`}>
          <JsonView value={contracts} defaultExpandDepth={0} maxHeight="max-h-64" />
        </Section>
      )}
    </div>
  );
}

function Section({
  label,
  count,
  defaultOpen,
  children,
}: {
  label: string;
  count: string;
  defaultOpen?: boolean;
  children: React.ReactNode;
}) {
  return (
    <Collapsible defaultOpen={defaultOpen}>
      <CollapsibleTrigger className="group flex w-full items-center gap-1.5 rounded px-1 py-1 text-left hover:bg-accent">
        <ChevronRight className="h-3 w-3 text-muted-foreground transition-transform group-data-[state=open]:rotate-90" />
        <span className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
          {label}
        </span>
        <span className="font-mono text-[10px] text-muted-foreground">{count}</span>
      </CollapsibleTrigger>
      <CollapsibleContent className="pt-1">{children}</CollapsibleContent>
    </Collapsible>
  );
}

/* ---------- one message in the array ---------- */

interface Message {
  role?: string;
  content?: unknown;
}

function MessageRow({ message, index }: { message: Message; index: number }) {
  const role = String(message.role ?? "?");
  const blocks = Array.isArray(message.content) ? message.content : [];

  return (
    <Collapsible>
      <CollapsibleTrigger className="group flex w-full items-center gap-2 px-2 py-1.5 text-left hover:bg-accent">
        <ChevronRight className="h-3 w-3 shrink-0 text-muted-foreground transition-transform group-data-[state=open]:rotate-90" />
        <span className="w-5 shrink-0 text-right font-mono text-[10px] text-muted-foreground">
          {index + 1}
        </span>
        <span
          className={cn(
            "w-16 shrink-0 rounded px-1.5 py-0.5 text-center font-mono text-[10px] font-semibold",
            role === "user"
              ? "bg-muted text-foreground"
              : "bg-primary/10 text-primary",
          )}
        >
          {role}
        </span>
        <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-muted-foreground">
          {previewOf(blocks)}
        </span>
      </CollapsibleTrigger>
      <CollapsibleContent>
        <div className="flex flex-col gap-1.5 border-t bg-muted/30 px-2 py-2">
          {blocks.map((b, i) => (
            <Block key={i} block={b} />
          ))}
          {blocks.length === 0 && (
            <div className="text-[11px] text-muted-foreground">empty content</div>
          )}
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}

function Block({ block }: { block: unknown }) {
  if (typeof block === "string") {
    return <TextViewer text={block} allowRendered={false} maxHeight="max-h-52" />;
  }
  if (!block || typeof block !== "object") return null;
  const b = block as Record<string, unknown>;

  if (typeof b.text === "string") {
    // Prompts and replies are the text the model actually read: raw first,
    // rendered available, same rule as the system-prompt drawer.
    return <TextViewer text={b.text} defaultView="raw" maxHeight="max-h-52" />;
  }

  const toolUse = (b.toolUse ?? b.tool_use) as Record<string, unknown> | undefined;
  if (toolUse) {
    return (
      <Labelled label={`→ ${String(toolUse.name ?? "tool")}`}>
        <JsonView value={toolUse.input ?? {}} maxHeight="max-h-40" />
      </Labelled>
    );
  }

  const toolResult = (b.toolResult ?? b.tool_result) as
    | Record<string, unknown>
    | undefined;
  if (toolResult) {
    const status = String(toolResult.status ?? "");
    return (
      <Labelled
        label={`← result${status ? ` (${status})` : ""}`}
        danger={status === "error"}
      >
        <JsonView
          value={toolResult.content ?? toolResult}
          maxHeight="max-h-40"
          isError={status === "error"}
        />
      </Labelled>
    );
  }

  return <JsonView value={b} maxHeight="max-h-40" />;
}

function Labelled({
  label,
  danger,
  children,
}: {
  label: string;
  danger?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div>
      <div
        className={cn(
          "mb-0.5 font-mono text-[10px] font-semibold",
          danger ? "text-destructive" : "text-muted-foreground",
        )}
      >
        {label}
      </div>
      {children}
    </div>
  );
}

/** One line that says what this message carries, without opening it. */
function previewOf(blocks: unknown[]): string {
  const parts: string[] = [];
  for (const block of blocks) {
    if (typeof block === "string") {
      parts.push(block);
      continue;
    }
    if (!block || typeof block !== "object") continue;
    const b = block as Record<string, unknown>;
    if (typeof b.text === "string") {
      parts.push(b.text.replace(/\s+/g, " ").trim());
      continue;
    }
    const toolUse = (b.toolUse ?? b.tool_use) as Record<string, unknown> | undefined;
    if (toolUse) {
      parts.push(`→ ${String(toolUse.name ?? "tool")}()`);
      continue;
    }
    if (b.toolResult ?? b.tool_result) {
      parts.push("← tool result");
    }
  }
  const line = parts.join("  ");
  return line.length > 160 ? `${line.slice(0, 160)}…` : line || "—";
}
