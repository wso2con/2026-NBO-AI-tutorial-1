import {
  BookOpen,
  Brain,
  Loader2,
  MessageSquare,
  Minimize2,
  Power,
  ShieldCheck,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { cn } from "@/lib/utils";
import type { AgentService, AgentTool } from "@/lib/api";
import type { AgentState, Turn } from "@/lib/types";
import { MemoryDrawer } from "./MemoryDrawer";
import { SystemPromptDrawer } from "./SystemPromptDrawer";
import { ToolsDrawer } from "./ToolsDrawer";
import { TurnCard } from "./TurnCard";

interface Props {
  service: AgentService;
  state: AgentState;
  tools: AgentTool[];
  /** Rendered system prompt from /api/tools. */
  systemPrompt: string;
  toolsLoading: boolean;
  toolsError: string | null;
  onToggleEnabled: () => void;
  /** Answers a turn's inline pause prompt (currently the token-budget
   *  continue/stop decision). */
  onAnswerPause?: (
    turn: Turn,
    approved: boolean,
    decisions?: Record<string, boolean>,
  ) => void;
  /** Disables the pause buttons while any agent is mid-stream. */
  pauseBusy?: boolean;
  // engineered-only: feature toggles. Undefined for first-cut — the row doesn't render.
  skillsEnabled?: boolean;
  episodicEnabled?: boolean;
  hitlEnabled?: boolean;
  compactAt?: number;
  onToggleSkills?: () => void;
  onToggleEpisodic?: () => void;
  onToggleHitl?: () => void;
  // Disables the feature toggles while a chat is in-flight (same as the
  // top-bar reset button).
  featuresDisabled?: boolean;
  // engineered-only: identifies whose memory file to read, and a bump counter the
  // parent increments after events that may have modified the file. Both
  // are required when `episodicEnabled` is true; otherwise unused.
  customerId?: string;
  memoryRefreshKey?: number;
}

export function AgentPanel({
  service,
  state,
  tools,
  systemPrompt,
  toolsLoading,
  toolsError,
  onToggleEnabled,
  onAnswerPause,
  pauseBusy,
  skillsEnabled,
  episodicEnabled,
  hitlEnabled,
  compactAt,
  onToggleSkills,
  onToggleEpisodic,
  onToggleHitl,
  featuresDisabled,
  customerId,
  memoryRefreshKey,
}: Props) {
  const lastTurn = state.turns[state.turns.length - 1];
  const status = lastTurn?.status ?? "idle";
  const lastDecision = [...(lastTurn?.loop_events ?? [])]
    .reverse()
    .find((event) => event.type === "loop_decision")?.decision;
  const displayStatus =
    status === "done" && typeof lastDecision === "string"
      ? lastDecision.toLowerCase()
      : status;
  const isRunning = status === "running";
  const isError = status === "error";
  const isComplete = displayStatus === "complete";

  // first-cut = amber accent, engineered = emerald. Used for the top stripe and the
  // status dot when the panel is idle / running so the columns are visually
  // distinct at a glance — even before the audience reads the labels.
  const accentClass = service.variant === "first_cut" ? "bg-first-cut" : "bg-engineered";
  const dotColor = isError ? "bg-destructive" : accentClass;

  const statusVariant: "secondary" | "destructive" | "success" = isError
    ? "destructive"
    : isComplete
      ? "success"
      : "secondary";

  return (
    <section
      className={cn(
        "relative flex h-full min-h-0 flex-col overflow-hidden rounded-xl border bg-card shadow-sm transition-opacity",
        !state.enabled && "opacity-50",
      )}
    >
      {/* first-cut/engineered accent stripe */}
      <div className={cn("h-[3px] w-full shrink-0", accentClass)} aria-hidden />

      {/* Header */}
      <div className="flex shrink-0 flex-wrap items-center gap-3 px-4 py-3">
        <span
          className={cn(
            "h-2.5 w-2.5 shrink-0 rounded-full text-current transition-shadow",
            dotColor,
            isRunning && "running-glow",
          )}
          aria-label={`${service.variant} status: ${displayStatus}`}
        />
        <div className="flex min-w-[150px] flex-1 flex-col">
          <div className="truncate text-sm font-semibold leading-tight tracking-tight">
            {service.label}
          </div>
          <div className="font-mono text-[11px] text-muted-foreground">
            {service.caption}
          </div>
        </div>
        <div className="ml-auto flex shrink-0 items-center gap-1.5">
          {isRunning && (
            <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
          )}
          {state.turns.length > 0 && (
            <Badge variant={statusVariant} className="capitalize">
              {displayStatus}
            </Badge>
          )}
          {/* Engineered-only build controls. Planner is kept in the global
              Controls popover because each agent has its own live setting. */}
          {onToggleSkills && (
            <FeatureToggle
              label="skills"
              icon={<BookOpen className="h-3 w-3" />}
              enabled={!!skillsEnabled}
              onClick={onToggleSkills}
              disabled={featuresDisabled}
            />
          )}
          {onToggleEpisodic && (
            <FeatureToggle
              label="memory"
              icon={<Brain className="h-3 w-3" />}
              enabled={!!episodicEnabled}
              onClick={onToggleEpisodic}
              disabled={featuresDisabled}
            />
          )}
          {onToggleHitl && (
            <FeatureToggle
              label="HITL"
              icon={<ShieldCheck className="h-3 w-3" />}
              enabled={!!hitlEnabled}
              onClick={onToggleHitl}
              disabled={featuresDisabled}
            />
          )}
          <label
            className={cn(
              "inline-flex cursor-pointer select-none items-center gap-1 rounded-md border px-2 py-1 text-xs transition-colors",
              state.enabled
                ? "border-transparent bg-secondary text-secondary-foreground hover:bg-secondary/80"
                : "border-input bg-background text-muted-foreground hover:bg-accent",
            )}
            title={state.enabled ? "Click to disable this agent" : "Click to enable"}
          >
            <input
              type="checkbox"
              checked={state.enabled}
              onChange={onToggleEnabled}
              className="sr-only"
            />
            <Power className="h-3 w-3" />
            <span className="font-medium">{state.enabled ? "on" : "off"}</span>
          </label>
        </div>
      </div>
      <Separator />

      {/* Capability banner. Both panels carry one so the two columns stay
          row-aligned; the badges say what each loop does and does not own. */}
      <div
        className={cn(
          "flex flex-wrap items-center gap-1 border-b px-4 py-2 text-[10px] uppercase tracking-wide text-muted-foreground",
          service.variant === "engineered" ? "bg-engineered/5" : "bg-first-cut/5",
        )}
      >
        <span
          className={cn(
            "mr-1 font-semibold",
            service.variant === "engineered" ? "text-engineered" : "text-first-cut",
          )}
        >
          {service.variant === "engineered" ? "harness" : "bare loop"}
        </span>
        {service.variant === "engineered" && Boolean(compactAt) && (
          <Badge
            variant="outline"
            className="gap-1 border-engineered/30 text-engineered"
            title={`Auto-compaction is active at ${compactAt! / 1000}k context tokens`}
          >
            <Minimize2 className="h-3 w-3" />
            compact {compactAt! / 1000}k
          </Badge>
        )}
        {(service.variant === "engineered"
          ? ["explicit run state", "progress control", "safe recovery"]
          : ["implicit run state", "no progress control", "no recovery"]
        ).map((capability) => (
          <Badge key={capability} variant="outline">
            {capability}
          </Badge>
        ))}
      </div>

      {/* Tools catalog drawer — collapsed by default */}
      <SystemPromptDrawer
        content={systemPrompt}
        loading={toolsLoading}
        error={toolsError ?? undefined}
      />

      <ToolsDrawer
        tools={tools}
        loading={toolsLoading}
        error={toolsError ?? undefined}
      />

      {/* Episodic-memory file viewer — only when the engineered feature is on.
          Lets the audience inspect what the agent committed to disk
          across the next-session boundary. */}
      {episodicEnabled && customerId && (
        <MemoryDrawer
          service={service}
          customerId={customerId}
          refreshKey={memoryRefreshKey ?? 0}
        />
      )}

      {/* Body — scrolling thread of turns */}
      <div className="scroll-thin flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-4">
        {state.turns.length === 0 ? (
          <div className="flex flex-1 flex-col items-center justify-center gap-2 text-center text-muted-foreground">
            <MessageSquare className="h-6 w-6 opacity-60" />
            {state.enabled ? (
              <div className="space-y-1">
                <div className="text-sm">Type a prompt below</div>
                <div className="text-xs">Both agents see the same input.</div>
              </div>
            ) : (
              <div className="space-y-1">
                <div className="text-sm">Disabled</div>
                <div className="text-xs">Won't receive prompts.</div>
              </div>
            )}
          </div>
        ) : (
          state.turns.map((t, i) => (
            <div key={t.id} className="flex flex-col gap-3">
              <TurnCard
                turn={t}
                isLatest={i === state.turns.length - 1}
                onAnswerPause={onAnswerPause}
                pauseBusy={pauseBusy}
              />
              {t.session_ended_after && (
                <div className="flex items-center gap-2 px-1 text-[10px] uppercase tracking-widest text-muted-foreground">
                  <div className="h-px flex-1 bg-border" />
                  <span>new session · time has passed</span>
                  <div className="h-px flex-1 bg-border" />
                </div>
              )}
            </div>
          ))
        )}
      </div>
    </section>
  );
}

interface FeatureToggleProps {
  label: string;
  icon: React.ReactNode;
  enabled: boolean;
  onClick: () => void;
  disabled?: boolean;
}

function FeatureToggle({
  label,
  icon,
  enabled,
  onClick,
  disabled,
}: FeatureToggleProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={
        disabled
          ? "Wait for the current turn to finish"
          : `Click to turn ${enabled ? "off" : "on"} (triggers a full reset)`
      }
      className={cn(
        "inline-flex items-center gap-1 rounded-md border px-2 py-1 text-xs transition-colors",
        "disabled:cursor-not-allowed disabled:opacity-50",
        enabled
          ? "border-transparent bg-secondary text-secondary-foreground hover:bg-secondary/80"
          : "border-input bg-background text-muted-foreground hover:bg-accent",
      )}
    >
      {icon}
      <span className="font-medium">{label}</span>
      <span className="ml-0.5 text-[10px] uppercase tracking-wider opacity-70">
        {enabled ? "on" : "off"}
      </span>
    </button>
  );
}
