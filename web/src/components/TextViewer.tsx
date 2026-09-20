import { useState } from "react";
import { cn } from "@/lib/utils";
import { CopyButton } from "@/components/ui/copy-button";
import { Markdown } from "./Markdown";

interface Props {
  text: string;
  /**
   * Which view opens first. Prompts default to "raw": the whole point of the
   * system-prompt drawer is that the audience sees the literal bytes the
   * model receives, headers and XML tags and all. Files the agent authored
   * for humans (episodic memory, skill bodies) default to "rendered".
   */
  defaultView?: "raw" | "rendered";
  /** Hide the toggle when the content is plainly not markdown. */
  allowRendered?: boolean;
  maxHeight?: string;
  className?: string;
}

/**
 * Viewer for the markdown-shaped text in the console — system prompts,
 * framed messages, memory files, skill bodies.
 *
 * Raw and rendered are both real answers, so the toggle is the answer: raw
 * shows what the model actually consumed, rendered shows the document a
 * human wrote. One click apart, with the token/line counts in the same bar.
 */
export function TextViewer({
  text,
  defaultView = "raw",
  allowRendered = true,
  maxHeight = "max-h-80",
  className,
}: Props) {
  const [view, setView] = useState(defaultView);
  const rendered = allowRendered && view === "rendered";
  const lines = text ? text.split("\n").length : 0;

  return (
    <div className={cn("overflow-hidden rounded-md border bg-background", className)}>
      <div className="flex items-center gap-2 border-b bg-muted/40 px-2 py-1">
        <span className="font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
          {lines.toLocaleString()} {lines === 1 ? "line" : "lines"} ·{" "}
          {text.length.toLocaleString()} chars
        </span>
        <div className="ml-auto flex items-center gap-0.5">
          {allowRendered && (
            <div className="flex overflow-hidden rounded border font-mono text-[10px]">
              {(["raw", "rendered"] as const).map((v) => (
                <button
                  key={v}
                  type="button"
                  onClick={() => setView(v)}
                  className={cn(
                    "px-1.5 py-0.5 transition-colors",
                    view === v
                      ? "bg-accent text-foreground"
                      : "text-muted-foreground hover:bg-accent/50",
                  )}
                >
                  {v}
                </button>
              ))}
            </div>
          )}
          <CopyButton value={text} title="Copy text" />
        </div>
      </div>
      <div className={cn("scroll-thin overflow-auto px-2.5 py-2", maxHeight)}>
        {rendered ? (
          <Markdown>{text}</Markdown>
        ) : (
          <pre className="whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed">
            {text}
          </pre>
        )}
      </div>
    </div>
  );
}
