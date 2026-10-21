import type { EventRecord } from "../api/types";

export type Density = "summary" | "normal" | "detailed";

export interface NodeGroup {
  id: string;
  node: string;
  label: string;
  status: string;
  startedAt: string | null;
  endedAt: string | null;
  durationMs: number | null;
  events: EventRecord[];
  toolCalls: Array<Record<string, unknown>>;
  error: string | null;
  summary: string | null;
}

export const NODE_LABELS: Record<string, string> = {
  api: "Task queue",
  worker: "Worker",
  understand_issue: "Issue analysis",
  retrieve: "Retrieval",
  localize: "Localization",
  localizer: "Localization",
  analyze_impact: "Impact analysis",
  build_env: "Environment & baseline",
  reproduce: "Reproduction",
  reproducer: "Reproduction",
  plan: "Repair plan",
  planner: "Repair plan",
  generate_patch: "Generate patch",
  repair_patch: "Iterative repair",
  coder: "Coding",
  validate_patch: "Patch validation",
  run_target_tests: "Target tests",
  run_regression: "Regression",
  review: "Independent review",
  reviewer: "Independent review",
  diagnose: "Failure diagnosis",
  diagnoser: "Failure diagnosis",
  check_budget: "Budget check",
  finalize: "Final report",
  recursion_limit: "Recursion limit",
  resume: "Resume",
};

const PRIMARY_NODES = new Set([
  "understand_issue",
  "retrieve",
  "localize",
  "analyze_impact",
  "build_env",
  "reproduce",
  "plan",
  "generate_patch",
  "repair_patch",
  "validate_patch",
  "run_target_tests",
  "run_regression",
  "review",
  "diagnose",
  "finalize",
  "worker",
  "api",
  "recursion_limit",
  "resume",
]);

export function nodeLabel(node: string): string {
  return NODE_LABELS[node] ? `stages.nodes.${node}` : node.replaceAll("_", " ");
}

// Agent-role events are emitted under role names (localizer, reviewer, ...),
// while the graph stage uses the node name (localize, review, ...). They describe
// one stage and must not become two sibling timeline entries.
const STAGE_FAMILIES: string[][] = [
  ["review", "reviewer"],
  ["localize", "localizer"],
  ["reproduce", "reproducer"],
  ["plan", "planner"],
  ["diagnose", "diagnoser"],
];

const AGENT_STAGE: Record<string, string> = {
  reviewer: "review",
  localizer: "localize",
  reproducer: "reproduce",
  planner: "plan",
  diagnoser: "diagnose",
};

const CODER_PARENTS = new Set(["generate_patch", "repair_patch"]);

function sameStageFamily(left: string, right: string): boolean {
  return STAGE_FAMILIES.some((family) => family.includes(left) && family.includes(right));
}

function stageNodeFor(node: string, current: NodeGroup | null): string {
  if (current && sameStageFamily(current.node, node)) return current.node;
  if (node === "coder" && current && CODER_PARENTS.has(current.node)) return current.node;
  return AGENT_STAGE[node] ?? node;
}

const IMPORTANT_STATUS = new Set([
  "error",
  "failed",
  "refused",
  "cancelled",
  "stop",
  "budget_exhausted",
  "uncertain",
]);

const SUMMARY_EVENTS = new Set([
  "node_end",
  "stop",
  "tool_call",
  "sandbox_run",
  "task_queued",
  "worker_started",
  "worker_finished",
  "worker_failed",
  "worker_recovered",
  "recovery_queued",
  "cancel_requested",
  "cancelled",
  "result_uncertain",
]);

const STATUS_EVENTS = new Set([
  "node_end",
  "stop",
  "tool_call",
  "sandbox_run",
  "task_queued",
  "worker_started",
  "worker_finished",
  "worker_failed",
  "worker_recovered",
  "recovery_queued",
  "cancel_requested",
  "cancelled",
  "result_uncertain",
]);

export function groupEvents(events: EventRecord[]): NodeGroup[] {
  const groups: NodeGroup[] = [];
  let current: NodeGroup | null = null;
  for (const event of [...events].sort((a, b) => a.seq - b.seq)) {
    const stageNode = stageNodeFor(event.node, current);
    if (!current || event.type === "node_start" || current.node !== stageNode) {
      current = {
        id: `${stageNode}-${event.seq}`,
        node: stageNode,
        label: nodeLabel(stageNode),
        status: "started",
        startedAt: event.timestamp,
        endedAt: null,
        durationMs: null,
        events: [],
        toolCalls: [],
        error: null,
        summary: null,
      };
      groups.push(current);
    }
    current.events.push(event);
    current.endedAt = event.timestamp;
    if (event.duration_ms !== null && event.duration_ms !== undefined) {
      current.durationMs = event.duration_ms;
    }
    if (event.tool_calls?.length) current.toolCalls.push(...event.tool_calls);
    if (event.error) current.error = event.error;
    if (event.summary && SUMMARY_EVENTS.has(event.type)) current.summary = event.summary;
    // A budget_check carries a machine-readable reading, not the node's outcome;
    // it must not replace the human summary or the completed status.
    if (event.status && (STATUS_EVENTS.has(event.type) || current.status === "started")) {
      current.status = event.status;
    }
  }
  return groups;
}

export function visibleEvents(group: NodeGroup, density: Density): EventRecord[] {
  if (density === "summary") return [];
  if (density === "detailed") return group.events;
  return group.events.filter((event) => {
    if (event.type === "llm_call" || event.type === "budget_check" || event.type === "node_start") {
      return false;
    }
    if (event.type === "node_end") {
      return Boolean(event.error) || Boolean(event.summary) || IMPORTANT_STATUS.has(event.status ?? "");
    }
    return true;
  });
}

export function visibleGroups(groups: NodeGroup[], density: Density): NodeGroup[] {
  // A completed run has a late `worker_finished` group. The early
  // `worker_started` group would otherwise keep saying "in progress" forever.
  const terminalWorker = groups.findIndex(
    (group) =>
      group.node === "worker" &&
      ["succeeded", "failed", "cancelled", "error", "uncertain"].includes(group.status),
  );
  const filtered = groups.filter(
    (group, index) =>
      !(
        terminalWorker >= 0 &&
        group.node === "worker" &&
        index < terminalWorker &&
        ["started", "running"].includes(group.status)
      ),
  );
  if (density !== "summary") return filtered;
  return filtered.filter((group) => {
    if (!PRIMARY_NODES.has(group.node)) return false;
    if (group.node === "check_budget" || group.node === "planner" || group.node === "coder") {
      return false;
    }
    if (group.node === "worker" || group.node === "api") {
      return IMPORTANT_STATUS.has(group.status) || group.status === "succeeded";
    }
    return true;
  });
}

export function groupTone(group: NodeGroup): "running" | "ok" | "warn" | "bad" | "idle" {
  if (group.status === "started" || group.status === "running") return "running";
  if (group.status === "error" || group.status === "failed" || group.status === "uncertain") {
    return "bad";
  }
  if (group.status === "skipped" || group.status.startsWith("budget_") || group.status === "stopped") {
    return "warn";
  }
  if (group.status === "cancelled" || group.status === "cancel_requested") return "warn";
  if (group.status === "ok" || group.status === "succeeded" || group.status === "continue") {
    return "ok";
  }
  return "idle";
}

const EVENT_KEYS = new Set([
  "node_start",
  "node_end",
  "llm_call",
  "tool_call",
  "sandbox_run",
  "budget_check",
  "stop",
  "task_queued",
  "worker_started",
  "worker_finished",
  "worker_failed",
  "cancel_requested",
  "cancelled",
  "recovery_queued",
  "worker_recovered",
  "result_uncertain",
]);

export function eventLabel(event: EventRecord): string {
  return EVENT_KEYS.has(event.type)
    ? `stages.events.${event.type}`
    : event.type.replaceAll("_", " ");
}

export function isStageStart(event: EventRecord): boolean {
  return event.type === "node_start" || event.type === "worker_started" || event.type === "task_queued";
}
