import { useState } from "react";
import { Check, Copy } from "lucide-react";
import { cn } from "@/lib/utils";

interface Props {
  /** Text placed on the clipboard. */
  value: string;
  className?: string;
  title?: string;
}

/**
 * Small ghost icon button that copies `value` and flips to a check for a
 * beat. Used in the viewers so a presenter can lift a system prompt or a
 * tool payload out of the console without selecting text on stage.
 */
export function CopyButton({ value, className, title = "Copy" }: Props) {
  const [copied, setCopied] = useState(false);

  return (
    <button
      type="button"
      title={title}
      onClick={(e) => {
        e.stopPropagation();
        void navigator.clipboard.writeText(value).then(() => {
          setCopied(true);
          setTimeout(() => setCopied(false), 1200);
        });
      }}
      className={cn(
        "rounded p-1 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground",
        className,
      )}
    >
      {copied ? (
        <Check className="h-3 w-3 text-success" />
      ) : (
        <Copy className="h-3 w-3" />
      )}
    </button>
  );
}
