import { useEffect } from "react";
import { X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { RichText } from "@/components/RichText";
import { cn } from "@/lib/utils";
import {
  scenariosBySection,
  type DemoScenario,
  type ScenarioPrompt,
} from "@/lib/scenarios";

interface Props {
  /** Called when the presenter clicks a scenario's prompt. */
  onApply: (scenario: DemoScenario, prompt: ScenarioPrompt, index: number) => void;
  /** Close the sidebar (e.g. from the header toggle, the X button, or Escape). */
  onClose: () => void;
  /** Disable prompt buttons while a turn is in flight. */
  disabled: boolean;
}

/**
 * Right-docked sidebar listing the lab's clickable demo scenarios. Stays
 * open while the presenter walks turns — clicking a prompt pre-fills the
 * composer and flips the session/model dropdowns but does NOT close the
 * panel, so multi-turn flows (e.g. memory scope leak, refund split) can
 * be driven from the same view. Close via the X, Escape, or the header
 * toggle.
 */
export function ScenariosPanel({ onApply, onClose, disabled }: Props) {
  // Escape closes the panel.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const groups = scenariosBySection();

  return (
    <aside
      className="flex h-full min-h-0 flex-col overflow-hidden rounded-xl border bg-card shadow-sm animate-fade-in"
      aria-label="Demo scenarios"
    >
      {/* Header */}
      <div className="flex shrink-0 items-center justify-between border-b px-3 py-3">
        <div className="flex flex-col leading-tight">
          <div className="text-sm font-semibold">Demo scenarios</div>
          <div className="text-[11px] text-muted-foreground">
            Click a prompt to fill the composer.
          </div>
        </div>
        <Button
          variant="ghost"
          size="icon"
          className="h-7 w-7"
          onClick={onClose}
          aria-label="Close scenarios sidebar"
        >
          <X className="h-3.5 w-3.5" />
        </Button>
      </div>

      {/* Sections */}
      <div className="scroll-thin flex min-h-0 flex-1 flex-col overflow-y-auto">
        {Array.from(groups.entries()).map(([sectionLabel, scenarios]) => (
          <div key={sectionLabel} className="border-b last:border-b-0">
            <div className="sticky top-0 z-10 border-b bg-card px-3 py-1.5 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
              {sectionLabel}
            </div>
            <div className="flex flex-col gap-2 px-3 py-2">
              {scenarios.map((s) => (
                <ScenarioCard
                  key={s.id}
                  scenario={s}
                  disabled={disabled}
                  onPrompt={(prompt, index) => onApply(s, prompt, index)}
                />
              ))}
            </div>
          </div>
        ))}
      </div>
    </aside>
  );
}

interface ScenarioCardProps {
  scenario: DemoScenario;
  disabled: boolean;
  onPrompt: (prompt: ScenarioPrompt, index: number) => void;
}

function ScenarioCard({ scenario, disabled, onPrompt }: ScenarioCardProps) {
  return (
    <div className="rounded-md border bg-muted/40 px-3 py-2 text-xs">
      <div className="flex flex-wrap items-center gap-1.5">
        <div className="font-semibold">{scenario.title}</div>
        {scenario.customer_id && (
          <Badge variant="outline" className="text-[10px]">
            {customerLabel(scenario.customer_id)}
          </Badge>
        )}
        {scenario.model && (
          <Badge variant="outline" className="font-mono text-[10px]">
            {scenario.model}
          </Badge>
        )}
      </div>
      <RichText text={scenario.goal} className="mt-1 text-muted-foreground" />
      <div className="mt-2 flex flex-col gap-1.5">
        {scenario.prompts.map((p, i) => (
          <button
            key={i}
            type="button"
            disabled={disabled}
            onClick={() => onPrompt(p, i)}
            className={cn(
              "rounded border bg-background px-2 py-1.5 text-left text-xs transition-colors hover:bg-accent hover:text-accent-foreground",
              "disabled:cursor-not-allowed disabled:opacity-50",
            )}
          >
            {p.label && (
              <div className="mb-0.5 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
                {p.label}
              </div>
            )}
            <div className="line-clamp-3 whitespace-pre-wrap leading-relaxed">{p.text}</div>
            {p.note && (
              <RichText
                text={p.note}
                className="mt-1 text-[10px] italic text-muted-foreground"
              />
            )}
          </button>
        ))}
      </div>
    </div>
  );
}

function customerLabel(id: string): string {
  if (id === "cust_001") return "Alice";
  if (id === "cust_002") return "Bob";
  if (id === "cust_003") return "Carol";
  return id;
}
