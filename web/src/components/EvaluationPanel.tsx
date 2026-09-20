import { Check, ChevronRight, X } from "lucide-react";
import type { EvaluationSuite } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { JsonView } from "./JsonView";

export function EvaluationPanel({ suite, onClose }: { suite: EvaluationSuite; onClose: () => void }) {
  return (
    <aside className="absolute inset-x-8 top-20 z-30 mx-auto max-w-4xl overflow-hidden rounded-xl border bg-card shadow-2xl">
      <div className="flex items-center gap-3 border-b px-4 py-3">
        <div>
          <div className="text-sm font-semibold">Validation suite</div>
          <div className="text-xs text-muted-foreground">Deterministic checks for outcomes, trajectories, and the reply-release gate</div>
        </div>
        <div className="ml-auto flex gap-4 font-mono text-xs">
          <span className="text-first-cut">First-cut {suite.summary.first_cut}/{suite.summary.total}</span>
          <span className="text-engineered">Engineered {suite.summary.engineered}/{suite.summary.total}</span>
        </div>
        <Button variant="ghost" size="icon" onClick={onClose}><X className="h-4 w-4" /></Button>
      </div>
      <div className="max-h-[65vh] divide-y overflow-y-auto">
        {suite.results.map((result) => (
          <Collapsible key={result.case}>
            <CollapsibleTrigger className="group grid w-full grid-cols-[1fr_120px_120px] items-center gap-3 px-4 py-3 text-left text-xs hover:bg-accent">
              <span className="flex items-center gap-2 font-medium"><ChevronRight className="h-3 w-3 transition-transform group-data-[state=open]:rotate-90" />{result.case}</span>
              <Outcome passed={result.first_cut.passed} />
              <Outcome passed={result.engineered.passed} />
            </CollapsibleTrigger>
            <CollapsibleContent>
              <div className="grid grid-cols-2 gap-4 border-t bg-muted/10 px-4 py-3 text-xs">
                <Expectation
                  title="First-cut expectation"
                  text={result.expected_behavior.first_cut}
                />
                <Expectation
                  title="Engineered expectation"
                  text={result.expected_behavior.engineered}
                />
              </div>
              <div className="grid grid-cols-2 gap-4 border-t bg-muted/20 px-4 py-3 text-xs">
                <AssertionList title="Engineered outcome assertions" items={result.outcome_assertions} />
                <AssertionList title="Engineered trajectory assertions" items={result.trajectory_assertions} />
              </div>
              <div className="border-t bg-muted/40 px-4 py-3">
                <div className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">evidence</div>
                <JsonView value={result.evidence} maxHeight="max-h-56" />
              </div>
            </CollapsibleContent>
          </Collapsible>
        ))}
      </div>
    </aside>
  );
}

function Expectation({ title, text }: { title: string; text: string }) {
  return (
    <div>
      <div className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">{title}</div>
      <p className="leading-relaxed">{text}</p>
    </div>
  );
}

function AssertionList({ title, items }: { title: string; items: Array<{ name: string; passed: boolean }> }) {
  return (
    <div>
      <div className="mb-2 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">{title}</div>
      <div className="space-y-1.5">
        {items.map((item) => (
          <div key={item.name} className="flex items-start gap-1.5">
            <span className={item.passed ? "text-engineered" : "text-destructive"}>{item.passed ? "✓" : "×"}</span>
            <span>{item.name}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function Outcome({ passed }: { passed: boolean }) {
  return passed ? (
    <span className="inline-flex items-center gap-1 text-engineered"><Check className="h-3.5 w-3.5" /> pass</span>
  ) : (
    <span className="text-destructive">× fail</span>
  );
}
