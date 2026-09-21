import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { BookOpen, Hourglass, RotateCcw, Sparkles, WifiOff } from "lucide-react";
import {
  AGENTS,
  DEFAULT_MODEL,
  SUPPORTED_MODELS,
  endSession,
  fetchSession,
  fetchTools,
  runAgent,
  stopPausedTask,
  resetAgent,
  type AgentService,
  type AgentTool,
  type SupportedModel,
} from "@/lib/api";
import {
  emptyAgentState,
  newTurn,
  restoredTurn,
  type AgentEvent,
  type AgentState,
  type AgentVariant,
  type EvaluationAspect,
  type Turn,
  type TurnEvaluation,
} from "@/lib/types";
import { AgentPanel } from "@/components/AgentPanel";
import { Composer } from "@/components/Composer";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { ScenariosPanel } from "@/components/ScenariosPanel";
import { ThemeToggle } from "@/components/ThemeToggle";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
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

// Session budget for the agent loop's total provider usage (input + output).
// Harness-side planner/reviewer calls and the policy MCP's one-time lookup are
// deliberately outside this number.
const DEFAULT_TOKEN_BUDGET = 40000;
const TOKEN_BUDGET_OPTIONS = [4000, 8000, 16000, 40000, 80000, 160000];

// engineered only. The line at which the harness summarizes the oldest
// messages instead of letting the next call's context keep growing, measured
// in the same projected input tokens the context bars are drawn in. 0 is off.
//
// The options are scaled to what this demo actually reaches. The agent's fixed
// surface (system prompt + tool contracts) measures ~2.6k tokens, and the mock
// backends return small observations, so even a deliberately broad "review my
// whole account" turn peaks near 4.3k. A line has to clear the fixed surface
// with room to be meetable at all, and has to sit under ~6k to be crossed
// inside a turn or two — otherwise the control looks broken on stage.
const DEFAULT_COMPACT_AT = 0;
const COMPACT_AT_OPTIONS = [0, 4000, 6000, 10000, 20000];

function resultIsError(result: unknown): boolean {
  if (result && typeof result === "object" && !Array.isArray(result)) {
    return "error" in result;
  }
  if (typeof result !== "string") return false;
  const lowered = result.toLowerCase();
  if (lowered.includes("error executing tool") || lowered.includes("service_timeout")) {
    return true;
  }
  try {
    const parsed = JSON.parse(result) as unknown;
    return Boolean(parsed && typeof parsed === "object" && !Array.isArray(parsed) && "error" in parsed);
  } catch {
    return false;
  }
}

function newRunId() {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
}

export default function App() {
  const [customerId, setCustomerId] = useState<string>("cust_001");
  const [selectedModel, setSelectedModel] = useState<SupportedModel>(DEFAULT_MODEL);
  const [tokenBudget, setTokenBudget] = useState(DEFAULT_TOKEN_BUDGET);
  const [compactAt, setCompactAt] = useState(DEFAULT_COMPACT_AT);
  // Composer text lives in App so the Scenarios panel can pre-fill it on click.
  const [composerText, setComposerText] = useState("");
  const [scenariosOpen, setScenariosOpen] = useState(false);
  const [selectedScenarioId, setSelectedScenarioId] = useState<string | null>(null);
  const [scenarioRunId, setScenarioRunId] = useState<string | null>(null);
  const [chatRunId, setChatRunId] = useState(newRunId);
  const [refundServiceTimeout, setRefundServiceTimeout] = useState(false);
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
          updateTurn(variant, turnId, (t) => {
            const row = {
              tool_use_id: ev.tool_use_id,
              name: ev.name,
              args: ev.args,
              args_summary: ev.args_summary,
            };
            // A resumed turn re-announces the calls that were still in flight
            // when it paused, so its trace reads on its own. Those rows are
            // already here — replace rather than append, or an approved write
            // shows up twice.
            const at = t.trace.findIndex((r) => r.tool_use_id === ev.tool_use_id);
            if (at === -1) return { ...t, trace: [...t.trace, row] };
            return {
              ...t,
              trace: t.trace.map((r, i) => (i === at ? { ...r, ...row } : r)),
            };
          });
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
                    // MCP can successfully transport a domain-level failure.
                    // Treat structured {error: ...} results as failed tool
                    // calls even when the protocol-level status is success.
                    is_error: ev.is_error || resultIsError(ev.result),
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
            usage: ev.usage ?? t.usage,
            pause: ev.pause ?? t.pause,
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
        case "human_approval":
        case "context_compacted":
        case "state_transition":
        case "loop_decision":
        case "recovery":
        case "reconciliation":
        case "completion":
          updateTurn(variant, turnId, (t) => ({
            ...t,
            run_id: ev.run_id,
            loop_events: [...t.loop_events, ev],
          }));
          break;

        // The post-turn review. These land after `done`, so the turn is
        // already marked complete and the reply is already rendered — they
        // only fill in the chip beside it.
        case "evaluation_started":
          updateTurn(variant, turnId, (t) => ({
            ...t,
            run_id: ev.run_id,
            loop_events: [...t.loop_events, ev],
            evaluation: {
              status: "pending",
              expected: Array.isArray(ev.aspects)
                ? (ev.aspects as TurnEvaluation["expected"])
                : [],
              aspects: [],
            },
          }));
          break;
        case "evaluation_aspect":
          updateTurn(variant, turnId, (t) => {
            const incoming = ev.evaluation as EvaluationAspect | undefined;
            if (!incoming) return t;
            const previous = t.evaluation ?? {
              status: "pending" as const,
              expected: [],
              aspects: [],
            };
            return {
              ...t,
              run_id: ev.run_id,
              loop_events: [...t.loop_events, ev],
              evaluation: {
                ...previous,
                // Aspects complete out of order, and a redelivered frame must
                // not double up, so replace by key rather than append blindly.
                aspects: [
                  ...previous.aspects.filter((a) => a.aspect !== incoming.aspect),
                  incoming,
                ],
              },
            };
          });
          break;
        case "evaluation_complete":
          updateTurn(variant, turnId, (t) => ({
            ...t,
            run_id: ev.run_id,
            loop_events: [...t.loop_events, ev],
            usage: (ev.usage as Turn["usage"]) ?? t.usage,
            evaluation: {
              status: "complete",
              verdict: ev.verdict as TurnEvaluation["verdict"],
              expected: t.evaluation?.expected ?? [],
              aspects: Array.isArray(ev.aspects)
                ? (ev.aspects as EvaluationAspect[])
                : (t.evaluation?.aspects ?? []),
            },
          }));
          break;
      }
    },
    [updateTurn],
  );

  /** Answer the inline pause on a turn that is holding work behind a human
   *  decision. Both kinds resume the SAME run on the agent that paused, and
   *  the other column's turn is finished and must not be replayed.
   *
   *  - budget: "Continue" grants more tokens and resumes the loop, adding
   *    nothing to the conversation. "Stop" drops the pause server side and
   *    runs nothing. It ends one turn and starts another, so it gets a turn
   *    of its own in the thread.
   *  - write confirmation: the loop is parked mid-tool-call, inside the turn
   *    already on screen. The decision goes back as structured data on the
   *    parked interrupts — the conversation the model reads never gains a
   *    question or an answer — so the turn simply continues where it stopped
   *    instead of a new exchange appearing in the chat. `decisions` carries
   *    one answer per queued write; approving one write never resumes
   *    another. */
  async function answerPause(
    variant: AgentVariant,
    turn: Turn,
    approved: boolean,
    decisions?: Record<string, boolean>,
  ) {
    const pause = turn.pause;
    if (!pause) return;
    const isBudget = pause.code === "budget_grant_required";
    updateTurn(variant, turn.id, (t) =>
      isBudget
        ? { ...t, pause_answered: approved ? "confirmed" : "rejected" }
        : {
            ...t,
            // The question is answered, so it stops being a pending pause and
            // becomes part of the turn's record. Appending rather than
            // replacing matters because a resumed turn can queue another write
            // and arrive with a fresh `pause`, which must not erase what the
            // customer already authorised.
            pause: undefined,
            approvals: [
              ...(t.approvals ?? []),
              ...(pause.awaiting ?? []).map((write) => ({
                write,
                approved: Boolean(decisions?.[write.id]),
              })),
            ],
          },
    );

    const svc = variant === "engineered" ? AGENTS.engineered : AGENTS.first_cut;

    if (isBudget && !approved) {
      void stopPausedTask(svc, {
        customer_id: customerId,
        run_id: pause.run_id,
      }).catch(() => undefined);
      return;
    }
    if (anyRunning) return;

    // A budget continuation is a fresh exchange in the thread. A write
    // confirmation is not: it resumes the turn the customer is looking at, so
    // its events stream back into that same card.
    const setState = variant === "engineered" ? setEngineered : setFirstCut;
    let streamTurnId = turn.id;
    let prompt = "";
    if (isBudget) {
      // Label only. The server takes the decision from the structured field
      // and adds no message to the conversation at all.
      prompt = `Continue (+${(pause.grant_tokens ?? 0).toLocaleString()} total tokens)`;
      const nextTurn = { ...newTurn(prompt), run_id: pause.run_id };
      streamTurnId = nextTurn.id;
      setState((s) => ({ ...s, turns: [...s.turns, nextTurn] }));
    } else {
      updateTurn(variant, turn.id, (t) => ({
        ...t,
        status: "running",
        streaming_reply: "",
        final_reply: "",
        error: null,
      }));
    }

    const controller = new AbortController();
    abortRef.current = controller;
    try {
      await runAgent(svc, {
        prompt,
        customer_id: customerId,
        model: selectedModel,
        run_id: pause.run_id,
        token_budget: tokenBudget,
        ...(isBudget
          ? { budget_grant: true }
          : decisions
            ? { confirm_decisions: decisions }
            : { confirm: approved }),
        ...(variant === "engineered"
          ? {
              skills_enabled: engineeredSkillsEnabled,
              episodic_enabled: engineeredEpisodicEnabled,
              planner_enabled: engineeredPlannerEnabled,
              compact_at: compactAt,
            }
          : { planner_enabled: firstCutPlannerEnabled }),
        signal: controller.signal,
        onEvent: handleEvent(variant, streamTurnId),
      });
      updateTurn(variant, streamTurnId, (t) =>
        t.status === "running" ? { ...t, status: "done" } : t,
      );
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      updateTurn(variant, streamTurnId, (t) => ({ ...t, status: "error", error: message }));
    }
  }

  async function send(prompt: string) {
    if (anyRunning) return;

    // Append a new turn to each ENABLED agent. Disabled ones simply skip.
    // Reuse one run id across messages in the same visible chat. That makes
    // the usage meter cumulative in exactly the same way as conversation
    // memory; reset/end-session/customer switch starts a new meter.
    const comparisonRunId = selectedScenarioId && scenarioRunId ? scenarioRunId : chatRunId;
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
          token_budget: tokenBudget,
          refund_service_timeout: refundServiceTimeout,
          ...(variant === "engineered"
            ? {
                skills_enabled: engineeredSkillsEnabled,
                episodic_enabled: engineeredEpisodicEnabled,
                planner_enabled: engineeredPlannerEnabled,
                compact_at: compactAt,
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
    // The UI switch arms one comparison run. Each backend keeps its own
    // one-shot fault armed until a qualifying refund consumes it.
    if (refundServiceTimeout) setRefundServiceTimeout(false);
    await Promise.allSettled(launches);
  }

  async function reset() {
    abortRef.current?.abort();
    // Clear chat history on both columns but keep their enabled toggles.
    setFirstCut((s) => ({ ...s, turns: [] }));
    setEngineered((s) => ({ ...s, turns: [] }));
    setChatRunId(newRunId());
    setScenarioRunId(null);
    setRefundServiceTimeout(false);
    setTokenBudget(DEFAULT_TOKEN_BUDGET);
    setCompactAt(DEFAULT_COMPACT_AT);
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
    setTokenBudget(DEFAULT_TOKEN_BUDGET);
    setCompactAt(DEFAULT_COMPACT_AT);
    setChatRunId(newRunId());
    setScenarioRunId(null);
    setRefundServiceTimeout(false);
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

  /** Called when the presenter clicks a scenario prompt in the panel.
   *  Pre-fills the composer; flips the model dropdown if the scenario
   *  specifies one. The customer dropdown is NOT auto-flipped — the
   *  scenario card shows the intended customer as a hint, but a manual
   *  selection in the header wins. Sending stays manual. */
  function applyScenario(scenario: DemoScenario, prompt: ScenarioPrompt) {
    setComposerText(prompt.text);
    if (scenario.id !== selectedScenarioId) {
      setScenarioRunId(newRunId());
    }
    setSelectedScenarioId(scenario.id);
    if (scenario.model) setSelectedModel(scenario.model);
    setRefundServiceTimeout(scenario.fault === "refund_service_timeout");
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
              onValueChange={(value) => {
                setCustomerId(value);
                setChatRunId(newRunId());
                setScenarioRunId(selectedScenarioId ? newRunId() : null);
              }}
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

          <Field label="total-token budget" htmlFor="token-budget">
            <Select
              value={String(tokenBudget)}
              onValueChange={(value) => setTokenBudget(Number(value))}
              disabled={anyRunning}
            >
              <SelectTrigger id="token-budget" className="w-[120px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {TOKEN_BUDGET_OPTIONS.map((value) => (
                  <SelectItem key={value} value={String(value)}>
                    {value < 1000 ? `${value} tokens` : `${value / 1000}k tokens`}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>

          <Field label="auto-compact at" htmlFor="compact-at">
            <Select
              value={String(compactAt)}
              onValueChange={(value) => setCompactAt(Number(value))}
              disabled={anyRunning}
            >
              <SelectTrigger id="compact-at" className="w-[120px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {COMPACT_AT_OPTIONS.map((value) => (
                  <SelectItem key={value} value={String(value)}>
                    {value === 0 ? "off" : `${value / 1000}k context`}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>

          <button
            type="button"
            disabled={anyRunning}
            onClick={() => setRefundServiceTimeout((enabled) => !enabled)}
            aria-pressed={refundServiceTimeout}
            title="One-shot fault: the next refund service call times out before commit"
            className={cn(
              "inline-flex h-9 items-center gap-1.5 rounded-md border px-2.5 text-xs transition-colors",
              "disabled:cursor-not-allowed disabled:opacity-50",
              refundServiceTimeout
                ? "border-amber-500/50 bg-amber-500/15 text-amber-700 dark:text-amber-400"
                : "border-input bg-background text-muted-foreground hover:bg-accent",
            )}
          >
            <WifiOff className="h-3.5 w-3.5" />
            timeout next refund
            <span className="text-[10px] uppercase tracking-wider opacity-70">
              {refundServiceTimeout ? "armed" : "off"}
            </span>
          </button>

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
              onAnswerPause={(turn, approved, decisions) =>
                answerPause("first_cut", turn, approved, decisions)
              }
              pauseBusy={anyRunning}
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
              onAnswerPause={(turn, approved, decisions) =>
                answerPause("engineered", turn, approved, decisions)
              }
              pauseBusy={anyRunning}
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
