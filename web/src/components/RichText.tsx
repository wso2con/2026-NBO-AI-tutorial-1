/**
 * Minimal inline-markdown renderer for short presenter-facing text in the
 * scenarios panel (goals, notes). Not a full markdown engine; just the
 * handful of marks that read poorly as raw characters in a tight UI card.
 *
 * Supported:
 *   `code`          → <code>
 *   **bold**        → <strong>
 *   blank line      → paragraph break
 *   line starting   → bullet list (consecutive `- ` lines group)
 *     with `- `
 *
 * Everything else is rendered as plain text. No HTML is parsed from the
 * source; the source is split + tokenized into safe React nodes.
 */
import { Fragment } from "react";
import { cn } from "@/lib/utils";

interface Props {
  text: string;
  className?: string;
}

export function RichText({ text, className }: Props) {
  const blocks = parseBlocks(text);
  return (
    <div className={cn("space-y-1.5", className)}>
      {blocks.map((b, i) =>
        b.type === "bullets" ? (
          <ul key={i} className="list-disc space-y-0.5 pl-4">
            {b.items.map((item, j) => (
              <li key={j}>{renderInline(item)}</li>
            ))}
          </ul>
        ) : (
          <p key={i} className="leading-relaxed">
            {renderInline(b.text)}
          </p>
        ),
      )}
    </div>
  );
}

type Block = { type: "para"; text: string } | { type: "bullets"; items: string[] };

function parseBlocks(text: string): Block[] {
  const lines = text.split("\n");
  const blocks: Block[] = [];
  let para: string[] = [];
  let bullets: string[] = [];

  const flushPara = () => {
    if (para.length) {
      blocks.push({ type: "para", text: para.join(" ").trim() });
      para = [];
    }
  };
  const flushBullets = () => {
    if (bullets.length) {
      blocks.push({ type: "bullets", items: bullets });
      bullets = [];
    }
  };

  for (const raw of lines) {
    const line = raw.trim();
    if (!line) {
      flushPara();
      flushBullets();
      continue;
    }
    if (line.startsWith("- ")) {
      flushPara();
      bullets.push(line.slice(2));
      continue;
    }
    flushBullets();
    para.push(line);
  }
  flushPara();
  flushBullets();
  return blocks;
}

/** Render inline marks: `code`, **bold**. */
function renderInline(text: string) {
  // Tokenize into a flat list alternating between plain and styled spans.
  // Single pass so the same character can't be matched twice.
  const tokens: { type: "text" | "code" | "bold"; value: string }[] = [];
  let i = 0;
  while (i < text.length) {
    const ch = text[i];
    if (ch === "`") {
      const end = text.indexOf("`", i + 1);
      if (end > i) {
        tokens.push({ type: "code", value: text.slice(i + 1, end) });
        i = end + 1;
        continue;
      }
    }
    if (ch === "*" && text[i + 1] === "*") {
      const end = text.indexOf("**", i + 2);
      if (end > i) {
        tokens.push({ type: "bold", value: text.slice(i + 2, end) });
        i = end + 2;
        continue;
      }
    }
    // accumulate plain text
    const last = tokens[tokens.length - 1];
    if (last && last.type === "text") {
      last.value += ch;
    } else {
      tokens.push({ type: "text", value: ch });
    }
    i += 1;
  }
  return tokens.map((t, k) => {
    if (t.type === "code") {
      return (
        <code
          key={k}
          className="rounded bg-muted px-1 py-0.5 font-mono text-[0.85em]"
        >
          {t.value}
        </code>
      );
    }
    if (t.type === "bold") {
      return (
        <strong key={k} className="font-semibold">
          {t.value}
        </strong>
      );
    }
    return <Fragment key={k}>{t.value}</Fragment>;
  });
}
