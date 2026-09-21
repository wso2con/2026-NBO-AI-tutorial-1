import ReactMarkdown from "react-markdown";
import remarkBreaks from "remark-breaks";
import { cn } from "@/lib/utils";

interface Props {
  children: string;
  /** "reply" is roomier; "compact" is for drawers and side panels. */
  density?: "reply" | "compact";
  className?: string;
}

/**
 * The one markdown surface in the console. Every place that renders agent-
 * or file-authored markdown (replies, episodic memory, skill bodies, the
 * rendered view of a system prompt) goes through here so headings, lists and
 * code blocks look the same everywhere and in both themes.
 */
export function Markdown({ children, density = "compact", className }: Props) {
  return (
    <div
      className={cn(
        "prose prose-sm max-w-none leading-relaxed dark:prose-invert",
        "prose-p:my-2 prose-headings:font-semibold prose-headings:my-2",
        "prose-ul:my-2 prose-ol:my-2 prose-li:my-0.5",
        "prose-code:rounded prose-code:bg-muted prose-code:px-1 prose-code:py-0.5 prose-code:font-normal prose-code:before:content-none prose-code:after:content-none",
        "prose-pre:my-2 prose-pre:bg-muted prose-pre:text-foreground prose-pre:border",
        "prose-hr:my-3 prose-blockquote:my-2 prose-blockquote:border-l-2",
        "prose-table:text-xs prose-th:px-2 prose-th:py-1 prose-td:px-2 prose-td:py-1",
        density === "compact" &&
          "prose-h1:text-sm prose-h2:text-xs prose-h2:uppercase prose-h2:tracking-wider prose-h2:text-muted-foreground prose-h3:text-xs",
        className,
      )}
    >
      <ReactMarkdown remarkPlugins={[remarkBreaks]}>{children}</ReactMarkdown>
    </div>
  );
}
