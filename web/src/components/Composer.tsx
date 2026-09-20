import { useEffect, useRef } from "react";
import { Send } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

interface Props {
  disabled: boolean;
  onSend: (text: string) => void;
  /** Controlled text — when set from outside (e.g. ScenariosPanel), the
   *  textarea updates in place. The local state still owns user keystrokes. */
  value: string;
  onChange: (value: string) => void;
}

export function Composer({ disabled, onSend, value, onChange }: Props) {
  const text = value;
  const setText = onChange;
  const ref = useRef<HTMLTextAreaElement>(null);

  // Auto-grow up to ~6 lines.
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 168)}px`;
  }, [text]);

  function submit() {
    const trimmed = text.trim();
    if (!trimmed || disabled) return;
    onSend(trimmed);
    setText("");
  }

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
      className={cn(
        "flex items-end gap-2 rounded-xl border bg-card p-2 shadow-sm transition-shadow",
        "focus-within:border-ring focus-within:ring-1 focus-within:ring-ring",
      )}
    >
      <label htmlFor="composer-input" className="sr-only">
        Prompt for both agents
      </label>
      <textarea
        id="composer-input"
        ref={ref}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            submit();
          }
        }}
        placeholder="ask both agents the same thing…  (Enter to send, Shift+Enter for newline)"
        rows={1}
        disabled={disabled}
        className={cn(
          "flex-1 resize-none bg-transparent px-3 py-2 text-sm leading-relaxed outline-none placeholder:text-muted-foreground",
          disabled && "opacity-60",
        )}
      />
      <Button
        type="submit"
        size="icon"
        disabled={disabled || !text.trim()}
        aria-label="Send prompt to both agents"
      >
        <Send className="h-4 w-4" />
      </Button>
    </form>
  );
}
