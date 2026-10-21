"""Self-contained static HTML report (A-11).

The page is generated from already-persisted artifacts. It does not call the API,
the model, Docker or the SWE-bench harness, and it never writes into a historical
experiment package. Jinja autoescaping is enabled for every rendered value,
including diffs and log lines.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, select_autoescape

from codepilot.api.schemas import PatchView, ReportView

__all__ = ["render_html_report", "write_html_report"]

_STAGES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("Issue analysis", "understand_issue", ("understand_issue",)),
    ("Retrieval", "retrieve", ("retrieve",)),
    ("Localization", "localize", ("localize", "analyzer")),
    ("Impact", "analyze_impact", ("analyze_impact",)),
    ("Environment & baseline", "build_env", ("build_env",)),
    ("Reproduction", "reproduce", ("reproduce", "reproducer")),
    ("Planning", "plan", ("plan", "planner")),
    ("Coding", "generate_patch", ("generate_patch", "repair_patch", "coder")),
    ("Patch validation", "validate_patch", ("validate_patch",)),
    ("Target tests", "run_target_tests", ("run_target_tests",)),
    ("Regression", "run_regression", ("run_regression",)),
    ("Review", "review", ("review", "reviewer")),
    ("Diagnosis", "diagnose", ("diagnose", "diagnoser")),
    ("Finalize", "finalize", ("finalize",)),
)


def _json_filter(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _stages(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for event in events:
        node = str(event.get("node") or "")
        if not node:
            continue
        current = latest.get(node)
        priority = _event_priority(event)
        if current is None or priority >= _event_priority(current):
            latest[node] = event
    output: list[dict[str, Any]] = []
    for label, key, nodes in _STAGES:
        found = [latest[node] for node in nodes if node in latest]
        if not found:
            status = "not_recorded"
            detail = "NOT RECORDED in the event log"
        else:
            last = found[-1]
            status = str(last.get("status") or "recorded")
            detail = str(last.get("summary") or last.get("error") or "")
        output.append({"label": label, "key": key, "status": status, "detail": detail})
    return output


def _event_priority(event: dict[str, Any]) -> int:
    return {
        "stop": 5,
        "node_end": 4,
        "budget_check": 3,
        "sandbox_run": 2,
        "tool_call": 1,
    }.get(str(event.get("type")), 0)


def _diff_lines(diff: str) -> list[dict[str, str]]:
    lines: list[dict[str, str]] = []
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---") or line.startswith("diff --git"):
            kind = "meta"
        elif line.startswith("@@"):
            kind = "hunk"
        elif line.startswith("+"):
            kind = "add"
        elif line.startswith("-"):
            kind = "del"
        else:
            kind = "ctx"
        lines.append({"kind": kind, "text": line})
    return lines


def _event_rows(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event in events:
        rows.append(
            {
                "seq": event.get("seq"),
                "type": event.get("type"),
                "node": event.get("node"),
                "status": event.get("status"),
                "summary": event.get("summary"),
                "error": event.get("error"),
                "timestamp": event.get("timestamp"),
                "extra": _json_filter(event.get("extra") or {}),
            }
        )
    return rows


_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CodePilot-Repair · {{ report.run_id }}</title>
<style>
:root{--bg:#faf9f7;--panel:#ffffff;--text:#24221f;--muted:#6f6b64;--line:#e6e2dc;
--accent:#4f6b5d;--soft:#eef2ef;--add:#e8f4ea;--del:#fbeceb;--warn:#8a5a1f;--bad:#8f3734}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:14px/1.55 Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1180px;margin:0 auto;padding:40px 28px 80px}
header{display:flex;justify-content:space-between;gap:24px;align-items:flex-start;
border-bottom:1px solid var(--line);padding-bottom:22px;margin-bottom:26px}
h1{font-size:24px;letter-spacing:-.02em;margin:0 0 6px}h2{font-size:15px;margin:0 0 12px}
h3{font-size:13px;margin:0 0 6px;color:var(--muted);font-weight:600;text-transform:uppercase;
letter-spacing:.06em}.muted{color:var(--muted)}.mono{font-family:"JetBrains Mono",ui-monospace,
SFMono-Regular,Consolas,monospace}
.badge{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);
border-radius:999px;padding:3px 10px;font-size:12px;background:var(--panel)}
.badge.ok{border-color:#bcd6c3;background:var(--soft)}.badge.warn{border-color:#e4d2b2;color:var(--warn)}
.badge.bad{border-color:#eccccc;color:var(--bad)}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}.panel{background:var(--panel);
border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:18px}
.kv{display:grid;grid-template-columns:160px 1fr;gap:7px 16px}.kv div{padding:2px 0}
.bar{height:6px;background:#eceae5;border-radius:4px;overflow:hidden;margin-top:6px}
.bar span{display:block;height:100%;background:var(--accent)}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:7px 8px;
border-bottom:1px solid var(--line);vertical-align:top}th{font-size:12px;color:var(--muted);
font-weight:600}td.num{text-align:right;white-space:nowrap}
.stage{display:flex;gap:12px;padding:8px 0;border-bottom:1px solid var(--line)}
.stage:last-child{border-bottom:0}.dot{width:8px;height:8px;border-radius:50%;background:#b9b4aa;
margin-top:6px;flex:none}.dot.ok{background:#5f8a6f}.dot.bad{background:#b15450}
.dot.warn{background:#b4833d}.dot.skip{background:#b9b4aa}
details{border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin:10px 0}
summary{cursor:pointer;font-weight:600}.pre{white-space:pre-wrap;word-break:break-word;
font-family:"JetBrains Mono",ui-monospace,Consolas,monospace;font-size:12px}
.diff{border:1px solid var(--line);border-radius:10px;overflow:auto;max-height:620px;
background:#fbfbfa}.diff div{white-space:pre;font:12px/1.55 "JetBrains Mono",ui-monospace,Consolas,monospace;
padding:0 12px}.diff .add{background:var(--add)}.diff .del{background:var(--del)}
.diff .hunk{background:#f0eee9;color:#5d5a53}.diff .meta{color:#817c73}
.small{font-size:12px}.tag{font-size:11px;border:1px solid var(--line);border-radius:6px;
padding:1px 6px;margin-left:6px;color:var(--muted)}
.error{color:var(--bad)}.footer{margin-top:36px;color:var(--muted);font-size:12px}
@media(max-width:820px){.grid{grid-template-columns:1fr}header{flex-direction:column}}
</style>
</head>
<body><main>
{% set closure = report.internal.closure %}
<header>
<div>
<h1>CodePilot-Repair run <span class="mono">{{ report.run_id }}</span></h1>
<div class="muted">{{ report.repository or "Repository not recorded" }} · base
<span class="mono">{{ report.base_commit or "unknown" }}</span> · generated {{ generated_at }}</div>
</div>
<div>
<span class="badge {{ status_class }}">{{ report.ui_status }}</span>
{% for tag in tags %}<span class="badge">{{ tag }}</span>{% endfor %}
</div>
</header>

{% if report.summary.simulated_llm or report.summary.simulated_sandbox %}
<div class="panel"><strong>Simulation boundary.</strong> This run used
{{ "FakeLLM" if report.summary.simulated_llm else "a live model" }} and
{{ "a fake sandbox" if report.summary.simulated_sandbox else "an execution sandbox" }}.
It is workflow evidence, not a real-model effectiveness result.</div>
{% endif %}

<div class="grid">
<div class="panel">
<h2>Outcome</h2>
<div class="kv">
<div class="muted">Raw status</div><div class="mono">{{ report.status }}</div>
<div class="muted">Stop reason</div><div class="mono">{{ report.stop_reason or "none" }}</div>
<div class="muted">Internal verification</div><div class="mono">{{ report.verification_level or "NOT RECORDED" }}</div>
<div class="muted">Official verdict</div><div>
{% if report.official.recorded %}<span class="mono">{{ report.official.verdict or "UNKNOWN" }}</span>
{% else %}<span class="muted">NOT RECORDED (no official harness report)</span>{% endif %}
</div>
<div class="muted">Harness status</div><div>{{ report.official.harness_status or "NOT RECORDED" }}</div>
</div>
<p class="small muted">Official and internal verdicts are independent. The official result
neither raises nor lowers the internal verification level.</p>
</div>

<div class="panel">
<h2>Budget & usage</h2>
<div class="kv">
<div class="muted">Tokens (in / out / cached)</div>
<div class="mono">{{ report.usage.input_tokens }} / {{ report.usage.output_tokens }} / {{ report.usage.cached_input_tokens }}</div>
<div class="muted">Cost</div><div class="mono">{{ "%.6f"|format(report.usage.cost) }} {{ report.usage.currency }}
{% if not report.usage.cost_known %}<span class="tag">UNKNOWN</span>{% endif %}</div>
<div class="muted">LLM calls</div><div class="mono">{{ report.usage.llm_calls }}</div>
<div class="muted">Test runs</div><div class="mono">{{ report.summary.test_runs or "NOT RECORDED" }}</div>
{% if closure.budget_usage %}
{% for name, value in closure.budget_usage.items() %}
<div class="muted">{{ name }}</div><div class="mono">{{ value }}</div>
{% endfor %}
{% endif %}
</div>
</div>
</div>

<div class="panel">
<h2>Workflow</h2>
{% for stage in stages %}
<div class="stage"><span class="dot {{ stage.dot }}"></span><div>
<div><strong>{{ stage.label }}</strong> <span class="tag">{{ stage.status }}</span></div>
{% if stage.detail %}<div class="small muted">{{ stage.detail }}</div>{% endif %}
</div></div>
{% endfor %}
</div>

<div class="grid">
<div class="panel">
<h2>Verification closure</h2>
{% if closure %}
{% for name, value in closure_rows %}
<div class="stage"><div><span class="muted">{{ name }}</span><div>{{ value }}</div></div></div>
{% endfor %}
{% if closure.unexecuted_stages %}
<h3>Unexecuted stages</h3>
<ul class="small">{% for item in closure.unexecuted_stages %}<li>{{ item }}</li>{% endfor %}</ul>
{% endif %}
{% else %}
<p class="muted">NOT APPLICABLE: no full-closure record was persisted.</p>
{% endif %}
</div>

<div class="panel">
<h2>Tests & review</h2>
<div class="kv">
<div class="muted">Target tests</div><div class="mono">{{ report.tests.target_tests_status or "NOT EXECUTED" }}</div>
<div class="muted">Regression complete</div><div class="mono">{{ report.tests.regression.complete if report.tests.regression.complete is not none else "NOT EXECUTED" }}</div>
<div class="muted">Regression levels</div><div class="mono">{{ report.tests.regression.levels | join(", ") if report.tests.regression.levels else "NOT EXECUTED" }}</div>
<div class="muted">New failures</div><div class="mono">{{ report.tests.regression.new_failures if report.tests.regression.new_failures is not none else "NOT RECORDED" }}</div>
<div class="muted">Review</div><div>
{% if report.review.executed %}{{ "approved" if report.review.approved else "not approved" }}
(high issues: {{ report.review.high_issues if report.review.high_issues is not none else "?" }}){% else %}NOT EXECUTED{% endif %}
</div>
<div class="muted">Review bound to candidate</div><div class="mono">{{ report.internal.review_bound_to_candidate if report.internal.review_bound_to_candidate is not none else "NOT RECORDED" }}</div>
</div>
{% if report.review.issues %}
<h3>Review issues</h3>
<ul class="small">{% for issue in report.review.issues %}<li>{{ issue.severity }} · {{ issue.file }}{% if issue.line %}:{{ issue.line }}{% endif %} — {{ issue.description }}</li>{% endfor %}</ul>
{% endif %}
</div>
</div>

{% if report.failures %}
<div class="panel"><h2>Recorded failures</h2>
<ul class="small">{% for item in report.failures %}<li>{{ item }}</li>{% endfor %}</ul></div>
{% endif %}

<div class="panel">
<h2>Patch{% if patch %} <span class="tag">{{ patch.files_count }} file(s), +{{ patch.additions }} −{{ patch.deletions }}</span>{% endif %}</h2>
{% if patch %}
<div class="kv small">
<div class="muted">Raw SHA-256</div><div class="mono">{{ patch.sha256 }}</div>
<div class="muted">Identity check</div><div>{{ "verified" if patch.identity_verified else "NOT VERIFIED" }}{% if patch.identity_note %} — {{ patch.identity_note }}{% endif %}</div>
</div>
{% for file in patch.files %}
<details><summary><span class="mono">{{ file.path }}</span>
<span class="tag">{{ file.status }}</span> <span class="tag">+{{ file.additions }} −{{ file.deletions }}</span></summary>
<div class="diff">{% for line in diff_files[file.path] %}<div class="{{ line.kind }}">{{ line.text }}</div>{% endfor %}</div>
</details>
{% endfor %}
{% else %}
<p class="muted">No final patch was recorded for this run.</p>
{% endif %}
</div>

<div class="panel">
<h2>Attempts</h2>
{% if report.attempts %}
<table><tr><th>#</th><th>Patch hash</th><th>Validation</th><th>Target tests</th><th>Failed</th><th>Review</th></tr>
{% for item in report.attempts %}<tr>
<td class="num">{{ item.iteration }}</td><td class="mono small">{{ item.patch_hash }}</td>
<td>{{ item.validation_passed }}</td><td>{{ item.target_status or "NOT EXECUTED" }}</td>
<td class="num">{{ item.tests_failed if item.tests_failed is not none else "—" }}</td>
<td>{{ item.review_approved if item.review_approved is not none else "NOT EXECUTED" }}</td>
</tr>{% endfor %}</table>
{% else %}<p class="muted">No attempts were recorded.</p>{% endif %}
</div>

<div class="panel">
<h2>Event log <span class="tag">{{ events | length }} events</span></h2>
<details><summary>Show raw event timeline</summary>
<table><tr><th>Seq</th><th>Type</th><th>Node</th><th>Status</th><th>Summary / error</th></tr>
{% for event in events %}
<tr><td class="num">{{ event.seq }}</td><td class="mono small">{{ event.type }}</td>
<td class="mono small">{{ event.node }}</td><td>{{ event.status or "" }}</td>
<td class="small">{{ event.summary or event.error or "" }}</td></tr>
{% endfor %}</table>
</details>
</div>

<footer class="footer">
Generated from persisted artifacts by <span class="mono">codepilot report --html</span>.
This page is read-only, works offline and does not re-run the model, Docker or the official harness.
</footer>
</main></body></html>
"""


def render_html_report(
    *,
    report: ReportView,
    events: list[dict[str, Any]],
    patch: PatchView | None,
    generated_at: datetime | None = None,
) -> str:
    environment = Environment(
        autoescape=select_autoescape(default_for_string=True, default=True),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = environment.from_string(_TEMPLATE)
    closure = report.internal.closure
    closure_rows = _closure_rows(closure)
    status_class = (
        "ok"
        if report.status in {"succeeded", "provisionally_verified"}
        else "bad"
        if report.status in {"failed", "error", "tests_failed", "sandbox_error"}
        else "warn"
    )
    tags: list[str] = []
    if report.stop_reason:
        tags.append(f"stop={report.stop_reason}")
    if closure.get("coding_stop_reason"):
        tags.append(f"coding_stop={closure['coding_stop_reason']}")
    if report.official.recorded:
        tags.append(f"official={report.official.verdict}")
    stage_rows = _stages(events)
    for row in stage_rows:
        row["dot"] = (
            "ok"
            if row["status"] in {"ok", "succeeded", "started"}
            else "bad"
            if row["status"] in {"failed", "error", "stop"}
            else "warn"
            if row["status"] in {"not_recorded", "skipped"}
            else ""
        )
    diff_files = {file.path: _diff_lines(file.diff) for file in (patch.files if patch else [])}
    return template.render(
        report=report,
        events=_event_rows(events),
        patch=patch,
        stages=stage_rows,
        closure_rows=closure_rows,
        diff_files=diff_files,
        generated_at=(generated_at or datetime.now(UTC)).isoformat(timespec="seconds"),
        status_class=status_class,
        tags=tags,
    )


def _closure_rows(closure: dict[str, Any]) -> list[tuple[str, str]]:
    if not closure:
        return []
    mapping: list[tuple[str, str]] = [
        ("Patch present", str(closure.get("patch_present"))),
        ("Candidate SHA-256", str(closure.get("candidate_patch_sha256") or "NOT RECORDED")),
        ("Frozen by coding", str(closure.get("frozen_by_coding_stage"))),
        ("Coding stop reason", str(closure.get("coding_stop_reason") or "none")),
        (
            "PatchValidator",
            "NOT EXECUTED"
            if not closure.get("validator_executed")
            else f"executed, passed={closure.get('validator_passed')}",
        ),
        (
            "Target tests",
            "NOT EXECUTED"
            if not closure.get("target_tests_executed")
            else f"{closure.get('target_tests_status')} on {str(closure.get('target_test_sha256'))[:16]}",
        ),
        (
            "Regression",
            (
                f"executed, complete={closure.get('regression_complete')}, "
                f"levels={closure.get('regression_levels')}, "
                f"new failures={closure.get('regression_new_failures')}"
                if closure.get("regression_executed")
                else "NOT EXECUTED"
            ),
        ),
        (
            "Reviewer",
            (
                f"executed, approved={closure.get('review_approved')}, "
                f"high issues={closure.get('review_high_issues')}, "
                f"bound to candidate={closure.get('review_bound_to_candidate')}"
                if closure.get("review_executed")
                else "NOT EXECUTED"
            ),
        ),
        (
            "Review calls",
            f"{closure.get('review_calls')} (post-review repair={closure.get('post_review_repair')})",
        ),
        (
            "Budget usage",
            ", ".join(f"{k}={v}" for k, v in (closure.get("budget_usage") or {}).items()),
        ),
        ("Cost hard cap", str(closure.get("cost_hard_cap") or "not set")),
    ]
    admission = closure.get("closure_admission")
    if isinstance(admission, dict):
        mapping.append(
            (
                "Closure admission",
                f"feasible={admission.get('feasible')}, required={admission.get('required')}, "
                f"remaining={admission.get('remaining')}, share={admission.get('coder_share')}, "
                f"trigger={admission.get('trigger')}",
            )
        )
    return mapping


def write_html_report(
    *,
    report: ReportView,
    events: list[dict[str, Any]],
    patch: PatchView | None,
    output: Path,
    generated_at: datetime | None = None,
) -> Path:
    rendered = render_html_report(
        report=report,
        events=events,
        patch=patch,
        generated_at=generated_at,
    )
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8", newline="\n")
    return output
