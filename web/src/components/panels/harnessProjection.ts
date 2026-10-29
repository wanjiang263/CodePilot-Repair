import type { SessionData } from "../../hooks/useSessionData";

// API projections carry the last applied event seq. Only later events can amend it.
export function mergeHarness(session: SessionData) {
  const harness = { ...(session.report?.harness ?? {}) };
  const projections = [harness.runtime ?? {}, session.task?.runtime ?? {}]
    .filter(value => !value.run_id || value.run_id === session.runId)
    .sort((a, b) => (a.event_seq ?? 0) - (b.event_seq ?? 0) || (a.revision ?? 0) - (b.revision ?? 0));
  const runtime = Object.assign({}, ...projections);
  let watermark = runtime.event_seq ?? 0;
  for (const event of [...session.events].sort((a, b) => a.seq - b.seq)) {
    if ((session.runId && event.run_id !== session.runId) || event.seq <= watermark) continue;
    watermark = event.seq;
    const extra = event.extra ?? {};
    const canonical = extra.runtime_projection as Record<string, any> | undefined;
    if (canonical && canonical.run_id === session.runId) {
      Object.assign(runtime, canonical);
      continue;
    }
    // Legacy events only; new events use the server's canonical projection.
    const facts = (extra.facts ?? {}) as Record<string, any>;
    if (extra.hook === "stage_start" || extra.hook === "stage_end") {
      const incoming = facts.runtime ?? {};
      if ((incoming.revision ?? 0) < (runtime.revision ?? 0)) continue;
      Object.assign(runtime, incoming);
    }
    if (extra.hook === "before_model") runtime.activity = "external_wait";
    if (["after_model", "before_tool", "after_tool", "exploration", "resource_acquired"].includes(String(extra.hook))) runtime.activity = "executing";
    if (extra.hook === "resource_wait") runtime.activity = "queued";
    if (extra.hook === "resource_acquired") runtime.resource_queued_seconds = (runtime.resource_queued_seconds ?? 0) + (facts.queued_seconds ?? 0);
    if (extra.hook === "after_tool") runtime.last_cache = facts.cache;
    if (extra.hook === "exploration" && (facts.new_relevant_evidence || facts.hypothesis_changed || facts.candidate_changed)) runtime.last_effective_progress = event.timestamp;
    if (["exploration", "candidate_changed"].includes(String(extra.hook)) && "has_modification" in facts) runtime.has_modification = facts.has_modification;
    if (extra.hook === "candidate_changed") runtime.last_effective_progress = event.timestamp;
    if (extra.hook === "resume") runtime.recovery = { reuse: facts.reuse, redo: facts.redo };
    if (extra.hook === "contract_ready") runtime.contract = facts.contract;
    if (extra.hook === "after_validation") { runtime.verification = facts.view; runtime.failure_summary = facts.failure_summary; }
    if (extra.hook === "stage_end") runtime.activity = "idle";
  }
  runtime.event_seq = watermark;
  if (!["queued", "running"].includes(session.status)) runtime.activity = "finished";
  return { harness, runtime };
}

