import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { BookOpen, Hourglass, RotateCcw, Sparkles, FlaskConical } from "lucide-react";
import {
  AGENTS,
  DEFAULT_MODEL,
  SUPPORTED_MODELS,
  endSession,
  fetchSession,
  fetchTools,
  runAgent,
  resetAgent,
  runEvaluationSuite,
  type AgentService,
  type AgentTool,
  type SupportedModel,
  type EvaluationSuite,
} from "@/lib/api";
import {
  emptyAgentState,
  newTurn,
  restoredTurn,
  type AgentEvent,
  type AgentState,
  type AgentVariant,
  type Turn,
} from "@/lib/types";
import { AgentPanel } from "@/components/AgentPanel";
import { Composer } from "@/components/Composer";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { EvaluationPanel } from "@/components/EvaluationPanel";
import { ScenariosPanel } from "@/components/ScenariosPanel";
import { ThemeToggle } from "@/components/ThemeToggle";
import { Button } from "@/components/ui/button";
import type { DemoScenario, ScenarioPrompt } from "@/lib/scenarios";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

const CUSTOMERS = [
  { id: "cust_001", label: "Alice · cust_001" },
  { id: "cust_002", label: "Bob · cust_002" },
  { id: "cust_003", label: "Carol · cust_003" },
];

const DEFAULT_TOOL_BUDGET = 12;
const TOOL_BUDGET_OPTIONS = [3, 4, 5, 8, 10, 12];

export default function App() {
  const [customerId, setCustomerId] = useState<string>("cust_001");
  const [selectedModel, setSelectedModel] = useState<SupportedModel>(DEFAULT_MODEL);
  const [toolBudget, setToolBudget] = useState(DEFAULT_TOOL_BUDGET);
  // Composer text lives in App so the Scenarios panel can pre-fill it on click.
  const [composerText, setComposerText] = useState("");
  const [scenariosOpen, setScenariosOpen] = useState(false);
  const [selectedScenarioId, setSelectedScenarioId] = useState<string | null>(null);
  const [scenarioRunId, setScenarioRunId] = useState<string | null>(null);
  const [firstCut, setFirstCut] = useState<AgentState>(emptyAgentState);
  const [engineered, setEngineered] = useState<AgentState>(emptyAgentState);
  // engineered-only feature toggles. `skills` / `episodic` default OFF so the engineered
  // panel starts as a bare-bones agent; the presenter flips them on during
  // the demo to show the lift each feature provides. Flipping skills or
  // episodic is destructive (rebuilds the agent) and triggers a full engineered
  // reset; `planner` is a per-request decision (the planner is a separate
  // LLM call, not part of the agent build) and flips freely without reset.
  const [engineeredSkillsEnabled, setEngineeredSkillsEnabled] = useState(false);
  const [engineeredEpisodicEnabled, setEngineeredEpisodicEnabled] = useState(false);
  const [engineeredPlannerEnabled, setEngineeredPlannerEnabled] = useState(false);
  // first-cut's planner uses the same shared `planner.py` module as engineered. first-cut has
  // no skills loader, so the planner always runs with skills_enabled=false.
  // Independent of engineered's toggle — flip per panel.
  const [firstCutPlannerEnabled, setFirstCutPlannerEnabled] = useState(false);
  // Holds the toggle the user is mid-flipping while the confirm dialog is
  // up. Cleared on confirm or cancel. Only `skills` / `episodic` need this
  // — planner has no rebuild and skips the dialog entirely.
  const [pendingV2Toggle, setPendingV2Toggle] = useState<
    { feature: "skills" | "episodic"; next: boolean } | null
  >(null);
  // Tool catalog per agent — fetched once on mount. Tools don't change
  // between requests, so we don't refetch on send/reset.
  const [firstCutTools, setFirstCutTools] = useState<AgentTool[]>([]);
  const [engineeredTools, setEngineeredTools] = useState<AgentTool[]>([]);
  // Counter the MemoryDrawer watches to know when to refetch the file.
  // Bumped after a engineered turn finishes, after reset, and after next-session.
  const [engineeredMemoryRefreshKey, setEngineeredMemoryRefreshKey] = useState(0);
  const bumpV2Memory = useCallback(
    () => setEngineeredMemoryRefreshKey((k) => k + 1),
    [],
  );
  const [firstCutToolsLoading, setFirstCutToolsLoading] = useState(true);
  const [engineeredToolsLoading, setEngineeredToolsLoading] = useState(true);
  // System prompts come from /api/tools alongside the tool list — both are
  // agent properties, not per-turn run output.
  const [firstCutSystemPrompt, setFirstCutSystemPrompt] = useState("");
  const [engineeredSystemPrompt, setEngineeredSystemPrompt] = useState("");
  const [firstCutToolsError, setFirstCutToolsError] = useState<string | null>(null);
  const [engineeredToolsError, setEngineeredToolsError] = useState<string | null>(null);
  const [resetMsg, setResetMsg] = useState<string | null>(null);
  const [evaluation, setEvaluation] = useState<EvaluationSuite | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  // Rehydrate each panel's thread from the agent's own session memory.
  // The console's copy of the conversation lives in React state, so a browser
  // refresh drops it while the agent still holds everything; this reads the
  // thread back from `agent.messages`. Runs at mount and whenever the
  // customer changes (engineered keeps one agent per customer), and never
  // while a turn is streaming, so it can't clobber live state.
  useEffect(() => {
    let cancelled = false;
    const restore = async (
      variant: AgentVariant,
      setter: typeof setFirstCut,
    ) => {
      try {
        const turns = await fetchSession(AGENTS[variant], customerId);
        if (cancelled) return;
        setter((prev) =>
          prev.turns.some((t) => t.status === "running")
            ? prev
            : { ...prev, turns: turns.map(restoredTurn) },
        );
      } catch {
        // A service that is down or predates /api/session simply leaves the
        // panel as it is — an empty thread is the correct fallback.
      }
    };
    void restore("first_cut", setFirstCut);
    void restore("engineered", setEngineered);
    return () => {
      cancelled = true;
    };
  }, [customerId]);

  // first-cut tools are static — fetch once at mount.
  useEffect(() => {
    let cancelled = false;
    fetchTools(AGENTS.first_cut)
      .then((catalog) => {
        if (cancelled) return;
        setFirstCutTools(catalog.tools);
        setFirstCutSystemPrompt(catalog.systemPrompt);
        setFirstCutToolsError(null);
      })
      .catch((err) => {
        if (cancelled) return;
        setFirstCutToolsError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (cancelled) return;
        setFirstCutToolsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // engineered tools depend on the feature toggles — refetch whenever they flip so
  // the drawer matches what the agent actually has registered.
  useEffect(() => {
    let cancelled = false;
    setEngineeredToolsLoading(true);
    fetchTools(AGENTS.engineered, {
      skills_enabled: engineeredSkillsEnabled,
      episodic_enabled: engineeredEpisodicEnabled,
    })
      .then((catalog) => {
        if (cancelled) return;
        setEngineeredTools(catalog.tools);
        setEngineeredSystemPrompt(catalog.systemPrompt);
        setEngineeredToolsError(null);
      })
      .catch((err) => {
        if (cancelled) return;
        setEngineeredToolsError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (cancelled) return;
        setEngineeredToolsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [engineeredSkillsEnabled, engineeredEpisodicEnabled]);

  const setters = useMemo(
    (): Record<AgentVariant, React.Dispatch<React.SetStateAction<AgentState>>> => ({
      first_cut: setFirstCut,
      engineered: setEngineered,
    }),
    [],
  );

  const lastTurnRunning = (s: AgentState) => {
    const last = s.turns[s.turns.length - 1];
    return last?.status === "running";
  };
  const anyRunning = lastTurnRunning(firstCut) || lastTurnRunning(engineered);

  const updateTurn = useCallback(
    (variant: AgentVariant, turnId: string, updater: (t: Turn) => Turn) => {
      setters[variant]((s) => ({
        ...s,
        turns: s.turns.map((t) => (t.id === turnId ? updater(t) : t)),
      }));
    },
    [setters],
  );

  const handleEvent = useCallback(
    (variant: AgentVariant, turnId: string) => (ev: AgentEvent) => {
      switch (ev.type) {
        case "system_prompt":
          updateTurn(variant, turnId, (t) => ({ ...t, system_prompt: ev.content }));
          break;
        case "user_message":
          updateTurn(variant, turnId, (t) => ({ ...t, framed_message: ev.content }));
          break;
        case "plan":
          updateTurn(variant, turnId, (t) => ({ ...t, plan: ev.content }));
          break;
        case "tool_call":
          updateTurn(variant, turnId, (t) => ({
            ...t,
            trace: [
              ...t.trace,
              {
                tool_use_id: ev.tool_use_id,
                name: ev.name,
                args: ev.args,
                args_summary: ev.args_summary,
              },
            ],
          }));
          break;
        case "tool_result":
          updateTurn(variant, turnId, (t) => ({
            ...t,
            trace: t.trace.map((r) =>
              r.tool_use_id === ev.tool_use_id
                ? {
                    ...r,
                    result: ev.result,
                    result_summary: ev.result_summary,
                    is_error: ev.is_error,
                  }
                : r,
            ),
          }));
          break;
        case "text_delta":
          updateTurn(variant, turnId, (t) => ({
            ...t,
            streaming_reply: t.streaming_reply + ev.delta,
          }));
          break;
        case "done":
          updateTurn(variant, turnId, (t) => ({
            ...t,
            status: "done",
            final_reply: ev.final_reply || t.streaming_reply,
          }));
          // The engineered agent may have appended to its episodic memory file
          // during this turn; nudge the MemoryDrawer to refetch.
          if (variant === "engineered") bumpV2Memory();
          break;
        case "error":
          updateTurn(variant, turnId, (t) => ({
            ...t,
            status: "error",
            error: ev.message,
          }));
          break;
        case "context_build":
        case "context_iteration":
        case "context_update":
        case "memory_retrieval":
        case "memory_write":
        case "skill_load":
        case "task_contract":
        case "llm_decision":
        case "action_selection":
        case "state_transition":
        case "loop_decision":
        case "recovery":
        case "reconciliation":
        case "validation":
        case "completion":
        case "evaluation":
          updateTurn(variant, turnId, (t) => ({
            ...t,
            run_id: ev.run_id,
            loop_events: [...t.loop_events, ev],
          }));
          break;
      }
    },
    [updateTurn],
  );

  async function send(prompt: string) {
    if (anyRunning) return;

    // Append a new turn to each ENABLED agent. Disabled ones simply skip.
    const freshRunId =
      typeof crypto !== "undefined" && "randomUUID" in crypto
        ? crypto.randomUUID()
        : `${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
    const comparisonRunId = selectedScenarioId && scenarioRunId ? scenarioRunId : freshRunId;
    const firstCutTurn = firstCut.enabled ? { ...newTurn(prompt), run_id: comparisonRunId } : null;
    const engineeredTurn = engineered.enabled ? { ...newTurn(prompt), run_id: comparisonRunId } : null;

    if (!firstCutTurn && !engineeredTurn) return; // both off → nothing to do

    if (firstCutTurn) setFirstCut((s) => ({ ...s, turns: [...s.turns, firstCutTurn] }));
    if (engineeredTurn) setEngineered((s) => ({ ...s, turns: [...s.turns, engineeredTurn] }));
    setResetMsg(null);

    const controller = new AbortController();
    abortRef.current = controller;

    const launch = async (svc: AgentService, variant: AgentVariant, turnId: string) => {
      try {
        await runAgent(svc, {
          prompt,
          customer_id: customerId,
          model: selectedModel,
          run_id: comparisonRunId,
          tool_budget: toolBudget,
          ...(variant === "engineered"
            ? {
                skills_enabled: engineeredSkillsEnabled,
                episodic_enabled: engineeredEpisodicEnabled,
                planner_enabled: engineeredPlannerEnabled,
              }
            : {
                planner_enabled: firstCutPlannerEnabled,
              }),
          signal: controller.signal,
          onEvent: handleEvent(variant, turnId),
        });
        // Servers may close the stream without an explicit `done`. Mark
        // as done if we never received one.
        updateTurn(variant, turnId, (t) =>
          t.status === "running" ? { ...t, status: "done" } : t,
        );
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        updateTurn(variant, turnId, (t) => ({ ...t, status: "error", error: message }));
      }
    };

    // Kick off engineered first — engineered has the MCP-subprocess spawn cost and the
    // AgentSkills injection on first request, so it's slower to issue the
    // first SSE chunk. first-cut fires immediately after; both still stream
    // concurrently, but the head start helps the two columns finish
    // visually in sync on the projector.
    const launches: Promise<void>[] = [];
    if (engineeredTurn) launches.push(launch(AGENTS.engineered, "engineered", engineeredTurn.id));
    if (firstCutTurn) launches.push(launch(AGENTS.first_cut, "first_cut", firstCutTurn.id));
    await Promise.allSettled(launches);
  }

  async function reset() {
    abortRef.current?.abort();
    // Clear chat history on both columns but keep their enabled toggles.
    setFirstCut((s) => ({ ...s, turns: [] }));
    setEngineered((s) => ({ ...s, turns: [] }));
    setToolBudget(DEFAULT_TOOL_BUDGET);
    setResetMsg("resetting…");
    try {
      await Promise.all([resetAgent(AGENTS.first_cut), resetAgent(AGENTS.engineered)]);
      // /api/reset wipes engineered's non-seed episodic-memory files, so the
      // drawer's cached content is stale.
      bumpV2Memory();
      setResetMsg("both agents reset");
      setTimeout(() => setResetMsg(null), 2500);
    } catch (err) {
      setResetMsg(`reset failed: ${err instanceof Error ? err.message : String(err)}`);
    }
  }

  function toggleEnabled(variant: AgentVariant) {
    setters[variant]((s) => ({ ...s, enabled: !s.enabled }));
  }

  /** Stage a engineered feature toggle behind the confirm dialog. Because skills
   *  and episodic memory are baked into the agent at build time
   *  (system_prompt / tools / plugins), the cached agent no longer matches
   *  the new toggle — so flipping requires a full engineered reset. */
  function toggleV2Feature(feature: "skills" | "episodic") {
    if (anyRunning) return;
    const current = feature === "skills" ? engineeredSkillsEnabled : engineeredEpisodicEnabled;
    setPendingV2Toggle({ feature, next: !current });
  }

  /** Confirm-dialog handler. Applies the staged toggle, then runs the same
   *  full reset as the top-bar button so behavior stays consistent. */
  async function confirmV2Toggle() {
    if (!pendingV2Toggle) return;
    const { feature, next } = pendingV2Toggle;
    setPendingV2Toggle(null);

    if (feature === "skills") setEngineeredSkillsEnabled(next);
    else setEngineeredEpisodicEnabled(next);

    await reset();
  }

  /** Simulate "time has passed" for the episodic-memory demo. Both agents'
   *  cached Agent is dropped (server-side and client-side conversation
   *  memory wiped), but engineered's episodic memory file on disk persists. A
   *  divider is appended in each panel's thread to mark the boundary. */
  async function nextSession() {
    if (anyRunning) return;
    setToolBudget(DEFAULT_TOOL_BUDGET);
    // Mark a divider after the last turn in each enabled panel.
    setFirstCut((s) => {
      if (s.turns.length === 0) return s;
      return {
        ...s,
        turns: s.turns.map((t, i) =>
          i === s.turns.length - 1 ? { ...t, session_ended_after: true } : t,
        ),
      };
    });
    setEngineered((s) => {
      if (s.turns.length === 0) return s;
      return {
        ...s,
        turns: s.turns.map((t, i) =>
          i === s.turns.length - 1 ? { ...t, session_ended_after: true } : t,
        ),
      };
    });
    setResetMsg("new session…");
    try {
      await Promise.all([
        endSession(AGENTS.first_cut, customerId),
        endSession(AGENTS.engineered, customerId),
      ]);
      // The file is preserved server-side, but a refetch confirms that
      // for the audience (and re-renders the drawer with the same chars).
      bumpV2Memory();
      setResetMsg("session ended, episodic memory preserved");
      setTimeout(() => setResetMsg(null), 3000);
    } catch (err) {
      setResetMsg(`end_session failed: ${err instanceof Error ? err.message : String(err)}`);
    }
  }

  async function evaluate() {
    if (anyRunning) return;
    setResetMsg("running deterministic evaluations…");
    try {
      const result = await runEvaluationSuite();
      setEvaluation(result);
      setResetMsg(null);
    } catch (err) {
      setResetMsg(`evaluation failed: ${err instanceof Error ? err.message : String(err)}`);
    }
  }

  /** Called when the presenter clicks a scenario prompt in the panel.
   *  Pre-fills the composer; flips the model dropdown if the scenario
   *  specifies one. The customer dropdown is NOT auto-flipped — the
   *  scenario card shows the intended customer as a hint, but a manual
   *  selection in the header wins. Sending stays manual. */
  function applyScenario(scenario: DemoScenario, prompt: ScenarioPrompt) {
    setComposerText(prompt.text);
    if (scenario.id !== selectedScenarioId) {
      setScenarioRunId(
        typeof crypto !== "undefined" && "randomUUID" in crypto
          ? crypto.randomUUID()
          : `${Date.now()}-${Math.random().toString(36).slice(2, 9)}`,
      );
    }
    setSelectedScenarioId(scenario.id);
    if (scenario.model) setSelectedModel(scenario.model);
  }

  return (
    <div className="flex h-full flex-col bg-aurora">
      {/* Top bar — single backdrop-blur for the whole page lives only here. */}
      <header className="sticky top-0 z-10 flex shrink-0 flex-wrap items-center gap-4 border-b bg-background/80 px-6 py-3 backdrop-blur-md">
        <div className="flex shrink-0 items-center gap-2">
          <div className="flex h-7 w-7 items-center justify-center rounded-md bg-primary text-primary-foreground shadow-sm">
            <Sparkles className="h-3.5 w-3.5" />
          </div>
          <div className="flex flex-col leading-tight">
            <div className="text-sm font-semibold tracking-tight">
              Inside the Agent Loop
            </div>
            <div className="text-[11px] text-muted-foreground">
              Same model. Same domain. Better execution system.
            </div>
          </div>
        </div>

        <div className="ml-auto flex flex-wrap items-center justify-end gap-3">
          <Field label="session" htmlFor="customer">
            <Select
              value={customerId}
              onValueChange={setCustomerId}
              disabled={anyRunning}
            >
              <SelectTrigger id="customer" className="w-[180px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {CUSTOMERS.map((c) => (
                  <SelectItem key={c.id} value={c.id}>
                    {c.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>

          <Field label="model" htmlFor="model">
            <Select
              value={selectedModel}
              onValueChange={(v) => setSelectedModel(v as SupportedModel)}
              disabled={anyRunning}
            >
              <SelectTrigger id="model" className="w-[160px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {SUPPORTED_MODELS.map((m) => (
                  <SelectItem key={m} value={m}>
                    {m}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>

          <Field label="tool budget" htmlFor="tool-budget">
            <Select
              value={String(toolBudget)}
              onValueChange={(value) => setToolBudget(Number(value))}
              disabled={anyRunning}
            >
              <SelectTrigger id="tool-budget" className="w-[110px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {TOOL_BUDGET_OPTIONS.map((value) => (
                  <SelectItem key={value} value={String(value)}>
                    {value} calls
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>

          <Button
            variant={scenariosOpen ? "secondary" : "outline"}
            size="sm"
            onClick={() => setScenariosOpen((o) => !o)}
            aria-pressed={scenariosOpen}
            aria-controls="scenarios-sidebar"
            title="Toggle demo scenarios sidebar"
          >
            <BookOpen className="mr-1 h-3.5 w-3.5" />
            scenarios
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={nextSession}
            disabled={anyRunning}
            title="Simulate 'time has passed'. Drops conversation memory on both sides; the engineered agent's episodic memory file persists."
          >
            <Hourglass className="mr-1 h-3.5 w-3.5" />
            end session
          </Button>
          <Button variant="outline" size="sm" onClick={evaluate} disabled={anyRunning}>
            <FlaskConical className="mr-1 h-3.5 w-3.5" />
            evaluate
          </Button>
          <Button variant="outline" size="sm" onClick={reset} disabled={anyRunning}>
            <RotateCcw className="mr-1 h-3.5 w-3.5" />
            reset
          </Button>
          <ThemeToggle />
          {resetMsg && (
            <span className="ml-1 text-xs text-muted-foreground animate-fade-in">
              {resetMsg}
            </span>
          )}
        </div>
      </header>

      {/* Body row: agent panels + composer on the left; scenarios sidebar
          docks full-height on the right when open so the composer shrinks
          to the panels' width instead of spanning under it. */}
      <div className="flex min-h-0 flex-1 gap-4 px-4 pt-4">
        <div className="flex min-h-0 flex-1 flex-col gap-4">
          <main className="grid min-h-0 flex-1 grid-cols-2 gap-4">
            <AgentPanel
              service={AGENTS.first_cut}
              state={firstCut}
              tools={firstCutTools}
              systemPrompt={firstCutSystemPrompt}
              toolsLoading={firstCutToolsLoading}
              toolsError={firstCutToolsError}
              onToggleEnabled={() => toggleEnabled("first_cut")}
              plannerEnabled={firstCutPlannerEnabled}
              onTogglePlanner={() => {
                // Planner toggle is per-request — no agent rebuild, no
                // confirm dialog, no reset. Same shape as engineered's planner
                // toggle, just independent state.
                if (anyRunning) return;
                setFirstCutPlannerEnabled((v) => !v);
              }}
              featuresDisabled={anyRunning}
            />
            <AgentPanel
              service={AGENTS.engineered}
              state={engineered}
              tools={engineeredTools}
              systemPrompt={engineeredSystemPrompt}
              toolsLoading={engineeredToolsLoading}
              toolsError={engineeredToolsError}
              onToggleEnabled={() => toggleEnabled("engineered")}
              skillsEnabled={engineeredSkillsEnabled}
              episodicEnabled={engineeredEpisodicEnabled}
              plannerEnabled={engineeredPlannerEnabled}
              onToggleSkills={() => toggleV2Feature("skills")}
              onToggleEpisodic={() => toggleV2Feature("episodic")}
              onTogglePlanner={() => {
                // Planner toggle is per-request — no agent rebuild, no
                // confirm dialog, no reset. The next /api/run picks up
                // the new value via the request body.
                if (anyRunning) return;
                setEngineeredPlannerEnabled((v) => !v);
              }}
              featuresDisabled={anyRunning}
              customerId={customerId}
              memoryRefreshKey={engineeredMemoryRefreshKey}
            />
          </main>
          <footer className="shrink-0 pb-4">
            <Composer
              disabled={anyRunning}
              onSend={send}
              value={composerText}
              onChange={setComposerText}
            />
          </footer>
        </div>
        {scenariosOpen && (
          <aside
            id="scenarios-sidebar"
            className="flex w-[380px] shrink-0 min-h-0 flex-col pb-4"
          >
            <ScenariosPanel
              onApply={applyScenario}
              onClose={() => setScenariosOpen(false)}
              disabled={anyRunning}
            />
          </aside>
        )}
      </div>

      <ConfirmDialog
        open={!!pendingV2Toggle}
        title={
          pendingV2Toggle
            ? `Turn ${pendingV2Toggle.next ? "on" : "off"} ${
                pendingV2Toggle.feature === "skills" ? "skills" : "episodic memory"
              }?`
            : ""
        }
        description="This triggers a full reset, same as the reset button. Both agents' chat history and mock data will be cleared, and the engineered agent's non-seed episodic memory will be wiped."
        confirmLabel="reset"
        destructive
        onConfirm={confirmV2Toggle}
        onCancel={() => setPendingV2Toggle(null)}
      />
      {evaluation && <EvaluationPanel suite={evaluation} onClose={() => setEvaluation(null)} />}
    </div>
  );
}

interface FieldProps {
  label: string;
  htmlFor: string;
  children: React.ReactNode;
}

function Field({ label, htmlFor, children }: FieldProps) {
  return (
    <div className="flex shrink-0 items-center gap-1.5">
      <label
        htmlFor={htmlFor}
        className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground"
      >
        {label}
      </label>
      {children}
    </div>
  );
}
