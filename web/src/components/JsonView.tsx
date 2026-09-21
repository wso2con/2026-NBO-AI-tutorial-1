import { useMemo, useState } from "react";
import { ChevronRight } from "lucide-react";
import { cn } from "@/lib/utils";
import { CopyButton } from "@/components/ui/copy-button";

/**
 * Structured viewer for the JSON that flows through the loop: tool args and
 * results, loop events, evaluation evidence.
 *
 * Why a tree instead of `JSON.stringify(v, null, 2)` in a <pre>: on a
 * projector the interesting field is usually three lines into a forty-line
 * blob. Collapsing containers and colouring scalars lets the presenter point
 * at the one key that matters, and multi-line strings (policy snippets,
 * skill bodies) render with their newlines intact instead of as `\n`.
 *
 * `raw` mode is always one click away — some of the point of the demo is
 * that the model sees bytes, not a pretty tree.
 */

interface Props {
  value: unknown;
  /** Depth that starts expanded. Deeper containers render collapsed. */
  defaultExpandDepth?: number;
  /** Max height of the scroll area, as a tailwind class. */
  maxHeight?: string;
  /** Render errors in destructive red. */
  isError?: boolean;
  className?: string;
}

export function JsonView({
  value,
  defaultExpandDepth = 2,
  maxHeight = "max-h-80",
  isError,
  className,
}: Props) {
  const [raw, setRaw] = useState(false);

  // Tool results arrive as strings that are often JSON themselves. Parse
  // them so the tree is useful; fall back to the string if it isn't JSON.
  const parsed = useMemo(() => coerceJson(value), [value]);
  const text = useMemo(
    () =>
      typeof parsed === "string" ? parsed : JSON.stringify(parsed, null, 2),
    [parsed],
  );
  const structured = parsed !== null && typeof parsed === "object";

  return (
    <div className={cn("overflow-hidden rounded-md border bg-background", className)}>
      <div className="flex items-center gap-1 border-b bg-muted/40 px-2 py-1">
        <span className="font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
          {structured ? describe(parsed) : typeof parsed}
        </span>
        <div className="ml-auto flex items-center gap-0.5">
          {structured && (
            <button
              type="button"
              onClick={() => setRaw((r) => !r)}
              className="rounded px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground hover:bg-accent hover:text-foreground"
              title={raw ? "Show the parsed tree" : "Show the raw JSON text"}
            >
              {raw ? "tree" : "raw"}
            </button>
          )}
          <CopyButton value={text} title="Copy JSON" />
        </div>
      </div>
      <div className={cn("scroll-thin overflow-auto px-2 py-1.5", maxHeight)}>
        {raw || !structured ? (
          <pre
            className={cn(
              "whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed",
              isError && "text-destructive",
            )}
          >
            {text}
          </pre>
        ) : (
          <div className="font-mono text-[11px] leading-relaxed">
            <Node value={parsed} depth={0} defaultExpandDepth={defaultExpandDepth} />
          </div>
        )}
      </div>
    </div>
  );
}

interface NodeProps {
  name?: string;
  value: unknown;
  depth: number;
  defaultExpandDepth: number;
  /** Trailing comma, so the tree still reads as JSON. */
  comma?: boolean;
}

function Node({ name, value, depth, defaultExpandDepth, comma }: NodeProps) {
  const isContainer = value !== null && typeof value === "object";
  const [open, setOpen] = useState(depth < defaultExpandDepth);

  if (!isContainer) {
    return (
      <div className="flex items-start gap-1 pl-4">
        {name !== undefined && <Key name={name} />}
        <Scalar value={value} />
        {comma && <span className="text-code-punct">,</span>}
      </div>
    );
  }

  const entries: [string, unknown][] = Array.isArray(value)
    ? value.map((v, i) => [String(i), v])
    : Object.entries(value as Record<string, unknown>);
  const [openB, closeB] = Array.isArray(value) ? ["[", "]"] : ["{", "}"];

  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-1 rounded px-0.5 text-left hover:bg-accent"
      >
        <ChevronRight
          className={cn(
            "h-3 w-3 shrink-0 text-muted-foreground transition-transform",
            open && "rotate-90",
          )}
        />
        {name !== undefined && <Key name={name} />}
        <span className="text-code-punct">{openB}</span>
        {!open && (
          <>
            <span className="px-1 text-[10px] text-muted-foreground">
              {entries.length} {entries.length === 1 ? "item" : "items"}
            </span>
            <span className="text-code-punct">{closeB}</span>
          </>
        )}
      </button>
      {open && (
        <>
          <div className="border-l border-border/60 pl-2 ml-1.5">
            {entries.map(([k, v], i) => (
              <Node
                key={k}
                name={Array.isArray(value) ? undefined : k}
                value={v}
                depth={depth + 1}
                defaultExpandDepth={defaultExpandDepth}
                comma={i < entries.length - 1}
              />
            ))}
            {entries.length === 0 && (
              <div className="pl-4 text-[10px] text-muted-foreground">empty</div>
            )}
          </div>
          <div className="pl-4 text-code-punct">
            {closeB}
            {comma ? "," : ""}
          </div>
        </>
      )}
    </div>
  );
}

function Key({ name }: { name: string }) {
  return (
    <span className="shrink-0 font-semibold text-code-key">
      {name}
      <span className="font-normal text-code-punct">:</span>
    </span>
  );
}

function Scalar({ value }: { value: unknown }) {
  if (value === null) return <span className="text-code-null">null</span>;
  if (typeof value === "number")
    return <span className="text-code-number">{String(value)}</span>;
  if (typeof value === "boolean")
    return <span className="text-code-bool">{String(value)}</span>;

  const text = String(value);
  // Multi-line payloads (policy text, skill bodies, framed messages) are the
  // interesting ones; show them as a wrapped block rather than one long line
  // with escaped newlines.
  if (text.includes("\n")) {
    return (
      <span className="min-w-0 whitespace-pre-wrap break-words text-code-string">
        {text}
      </span>
    );
  }
  return (
    <span className="min-w-0 break-words text-code-string">"{text}"</span>
  );
}

/** "3 keys" / "5 items" — the one-line shape of a container. */
function describe(value: unknown): string {
  if (Array.isArray(value)) {
    return `array · ${value.length} ${value.length === 1 ? "item" : "items"}`;
  }
  const n = Object.keys(value as object).length;
  return `object · ${n} ${n === 1 ? "key" : "keys"}`;
}

/**
 * Tool results cross the wire as strings. If a string is really a JSON
 * document, parse it so it gets the tree; otherwise leave it alone.
 */
export function coerceJson(value: unknown): unknown {
  if (typeof value !== "string") return value;
  const trimmed = value.trim();
  if (!trimmed.startsWith("{") && !trimmed.startsWith("[")) return value;
  try {
    return JSON.parse(trimmed);
  } catch {
    return value;
  }
}
