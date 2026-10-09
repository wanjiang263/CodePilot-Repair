# CodePilot-Repair AGENTS.md

> Version: v1.1  
> Audience: coding agents (Codex, Claude Code, etc.) implementing this project.  
> Source of truth: requirements in `01-需求文档-PRD.md`, design in `02-实现方案.md`, phases and acceptance criteria in `04-分阶段开发任务清单.md`. If this file conflicts with 02 on a design detail, 02 wins; if it conflicts on a process rule, this file wins.  
> When copied to the repository root as `AGENTS.md`, keep it in sync with this file.

## 1. Mission

You are developing **CodePilot-Repair**, a repository-level autonomous software engineering agent.

The system receives:

- a Git repository;
- a fixed commit;
- a software issue;

and attempts to:

```text
understand
→ retrieve
→ localize
→ analyze impact
→ build environment + test baseline
→ reproduce the issue
→ plan
→ patch
→ execute
→ diagnose failure
→ repair
→ regression test
→ review
→ produce verified patch
```

The project is intended to be a serious AI Agent / software engineering research project, not a toy "LLM + RAG + code generation" demo.

---

# 2. Mandatory Engineering Principles

## 2.1 Incremental implementation

Never implement the entire project in one step.

Always work phase by phase.

Before implementing a phase:

1. inspect the current repository;
2. inspect existing architecture;
3. identify dependencies;
4. identify interfaces that already exist;
5. implement the smallest coherent change;
6. write tests;
7. run tests;
8. fix failures;
9. update documentation.

---

## 2.2 No fake implementations

Do not create fake implementations for core functionality.

Do not write:

```python
return []
```

or:

```python
raise NotImplementedError
```

and then claim the feature is complete.

Temporary stubs are allowed only when:

- clearly named;
- isolated behind an interface;
- explicitly documented;
- covered by tests;
- not presented as production functionality.

---

# 3. Language Scope

Version 1 supports **Python repositories only**.

Do not add Java, C++, JavaScript, Go, Rust, or other language support unless explicitly requested.

---

# 4. Core Architecture

The system must contain these conceptual components:

```text
Repository Intelligence
    ├── Repository Scanner
    ├── Tree-sitter Parser
    ├── File Index
    ├── Symbol Index
    ├── Call Graph
    ├── Dependency Graph
    ├── Test Index
    ├── Git History
    └── Retrieval

Agent Runtime
    ├── Issue Analyzer
    ├── Issue Localizer
    ├── Impact Analyzer
    ├── Planner
    ├── Reproduction Agent
    ├── Coding Agent
    ├── Failure Analyzer / Diagnosis Agent
    ├── Repair Controller (budget, progress, stop rules)
    └── Patch Reviewer

LLM Layer
    ├── Provider interface
    ├── Structured output + retry
    ├── Record / Replay
    └── Cost tracking

Execution
    ├── Docker Sandbox
    ├── Environment Builder + image cache
    ├── Test Runner (junit)
    ├── Test Baseline
    ├── Resource Limits
    └── Artifact Manager

Evaluation
    ├── Benchmark Loader (inference / evaluation split)
    ├── Benchmark Runner
    ├── Metrics
    ├── Ablation + statistics
    └── Experiment Report

Observability
    ├── Events
    ├── Traces
    ├── Token Usage / Cost
    └── Latency
```

Package layout is in 02「目录结构」.

---

# 5. Agent Architecture

Use LangGraph for the explicit state machine. The authoritative node list and routing table are in 02「LangGraph 编排」. Conceptually:

```text
START
 ↓
UNDERSTAND_ISSUE → RETRIEVE → LOCALIZE → ANALYZE_IMPACT
 ↓
BUILD_ENV (env image + test baseline)
 ↓
REPRODUCE (optional; degrade gracefully on failure)
 ↓
PLAN → GENERATE_PATCH → VALIDATE_PATCH
 ├── invalid → CHECK_BUDGET
 └── valid   → RUN_TARGET_TESTS
                 ├── PASS → RUN_REGRESSION
                 │            ├── no new failures → REVIEW
                 │            │                      ├── approved → FINALIZE
                 │            │                      └── high-severity issue → CHECK_BUDGET
                 │            └── new failures → DIAGNOSE
                 ├── FAIL / TIMEOUT / COLLECTION_ERROR → DIAGNOSE
                 └── ENV_ERROR / SANDBOX_ERROR → FINALIZE
DIAGNOSE → CHECK_BUDGET
CHECK_BUDGET
 ├── stop condition → FINALIZE
 └── continue → REPAIR_PATCH → VALIDATE_PATCH
```

Rules:

- Each node is a separate function `async def node(state, deps) -> dict` returning only the fields it updates.
- Dependencies (LLM, sandbox, index, event sink) are injected, never imported as globals.
- Routing functions are pure and unit-tested.
- Do not hide this workflow inside a single large function.

---

# 6. RepairState

Agent state is a Pydantic model and must round-trip through JSON. The full definition is in 02「LangGraph 编排 / RepairState」. Key groups:

```text
identity     task_id, run_id, repository_id, base_commit, issue, mode
analysis     issue_analysis, retrieval, localization, impact, plan
environment  baseline, reproduction
history      attempts[]  (patch, validation, execution, failure signatures, diagnosis, progress)
outcome      current_patch, regression, review, status, stop_reason
accounting   budget, usage (tokens, cost, llm_calls), test_runs, started_at
```

- Store large payloads (full logs, full retrieval output) as artifact paths, not inline.
- Never overwrite history; append a new `Attempt` per iteration.
- Do not use global mutable state.

---

# 7. Repository Intelligence

## 7.1 Scanner

Ignore:

```text
.git
.venv
venv
__pycache__
.pytest_cache
.mypy_cache
dist
build
node_modules
```

unless explicitly required.

## 7.2 Parser

Use Tree-sitter for structural parsing.

At minimum identify:

- modules;
- classes;
- functions;
- methods;
- calls;
- imports;
- tests.

## 7.3 Symbol Index

Every symbol should contain:

```text
name
qualified_name
type
file
start_line
end_line
signature
```

## 7.4 Graph

Support:

```text
CALL
IMPORT
INHERIT
REFERENCE
TEST
```

Static analysis is allowed to be approximate.

Do not block the entire project trying to achieve perfect Python type resolution.

---

# 8. Retrieval

Do not rely on vector search alone.

Use:

```text
Lexical
+
Semantic
+
Structural
+
Test proximity
+
Git history
```

Candidate ranking must be explicit.

- Normalize each channel's scores within the query's candidate set before combining. Never add raw BM25 scores to cosine similarities.
- Implement both weighted fusion and RRF; keep per-channel scores on every candidate for explanation.
- Repository-internal symbols that appear in an issue traceback are pinned to the top and skip fusion.
- Each channel must be independently switchable via config (needed for ablation).
- Tune weights only on the synthetic set and the SWE-bench dev subset, never on the reported test subset.

Default V1 weights:

```text
semantic       0.30
lexical        0.20
structural     0.20
test           0.15
history        0.15
```

These are configurable defaults, not scientifically validated constants.

---

# 9. Context Management

Do not send the entire repository to the LLM.

Use:

```text
Issue
→ Top-K candidates
→ Structural expansion
→ Relevant tests
→ Relevant code
→ Limited context
```

Always respect context and token budgets.

---

# 10. Planning Agent

The Planner:

- reasons about likely root cause;
- selects files/symbols;
- proposes minimal changes;
- identifies tests;
- estimates risk.

The Planner MUST NOT modify files.

---

# 11. Coding Agent

The Coding Agent may use only controlled tools:

```text
read_file
search_code
find_symbol
get_callers
get_callees
get_related_tests
edit_file        search/replace block, the primary edit format
create_file
get_git_diff
run_tests        sandboxed; counts against max_test_runs
finish
```

Do not expose unrestricted host shell execution.

- Every tool has a Pydantic input and output schema and its own unit tests.
- Resolve every path argument and reject anything outside the workspace (`..`, absolute paths, symlink escape).
- `edit_file` requires an exact, unique match. On failure return the three closest snippets so the agent can retry; on multiple matches return all locations.
- Cap tool calls per iteration (`max_tool_calls_per_iteration`).

## 11.1 Reproduction Agent

- Writes a test under `tests/_codepilot_repro/` that should fail on the base commit for a reason related to the issue.
- A reproduction is valid only if it fails on base with `AssertionError` or the exception type named in the issue. `ImportError` / `SyntaxError` do not count.
- Retry up to 3 times; on failure, continue without it and record `reproduction=failed`.
- Strip `tests/_codepilot_repro/` from the final patch.

## 11.2 Information isolation

This is a hard rule. Breaking it invalidates every reported number.

During inference the agent may see only: the issue text, the repository at the base commit (including existing tests), and git history up to the base commit.

It must never see: gold patches, evaluation test patches, FAIL_TO_PASS / PASS_TO_PASS lists, commits after the base commit, or SWE-bench `hints_text` (unless an experiment explicitly enables it and reports that).

Implementation: split loaded data into `InferenceTask` and `EvaluationTask` immediately; inference modules never import evaluation modules, enforced by a test.

---

# 12. Patch Policy

Every patch must be validated before execution.

Validate:

1. file paths (inside workspace; not `.git/`, `.github/`, `conftest.py`, `pytest.ini`, `tox.ini`, `setup.cfg`, `pyproject.toml` by default);
2. syntax (`ast.parse` on every changed `.py` file);
3. changed-file count;
4. changed-line count;
5. non-empty, not whitespace/comment-only;
6. no edits to existing test files unless `allow_test_edits=true`;
7. cheating patterns, detected by AST comparison, not string matching.

Rejections are returned to the Coding Agent with the reason and count as an iteration.

Reject patches that attempt to:

```text
delete failing tests
weaken assertions
skip tests
mark failing tests as xfail
hardcode expected output
disable validation
```

unless the issue itself legitimately requires such changes and the reviewer explicitly approves them.

---

# 13. Docker Sandbox

All repository code execution MUST occur inside a controlled Docker sandbox.

Two phases (details in 02「Sandbox 与环境构建」):

```text
environment build   network allowed, runs once, result cached as an image
                    keyed by sha1(repo, base_commit, env_spec)
test execution      network = none, one fresh container per run
```

Test execution defaults:

```text
network      = none
CPU          = 2
memory       = 4GB (no swap)
pids         = 512
timeout      = 300 seconds, then kill + force remove
user         = non-root (1000:1000)
root fs      = read-only; writable /workspace, /out, tmpfs /tmp
capabilities = drop ALL, no-new-privileges
```

- Read test outcomes from `--junitxml`, not from terminal text.
- Label containers `codepilot=1` and clean up stale ones on startup.
- Docker tests are marked `@pytest.mark.docker` and skipped when Docker is unavailable.
- Never execute untrusted repository commands directly on the host.

The sandbox is an evaluation sandbox, not a claim of production-grade isolation against arbitrary hostile workloads.

---

# 14. Execution

Execution results must contain (full schema in 02「ExecutionResult」):

```text
status
exit_code
duration_seconds
tests[]          per-test node_id, outcome, duration, message, traceback
tests_passed
tests_failed
stdout_tail      truncated; full log stored as artifact
stderr_tail
artifact_dir
```

Possible status:

```text
PASS
FAIL
TIMEOUT
COLLECTION_ERROR   syntax / import errors during collection
ENV_ERROR          environment problem, not caused by the patch
SANDBOX_ERROR      Docker / container failure
```

`ENV_ERROR` and `SANDBOX_ERROR` never enter the diagnose-repair loop. They end the run and are reported separately.

Before any repair, run the full test suite twice on the base commit to build a `TestBaseline` (per-test outcome + flaky set). All regression decisions compare against it.

---

# 15. Failure Diagnosis

Failure processing must be layered:

```text
raw output
 ↓
parser
 ↓
failure classifier
 ↓
relevant stack trace
 ↓
relevant source
 ↓
LLM diagnosis
```

Do not send huge raw logs to the LLM unnecessarily.

- Rule-based classification first; call the LLM only for `UNKNOWN`.
- Compute a `FailureSignature` (exception type + deepest in-repo frame + normalized message) for repeat detection.
- Compress: dedupe by signature, keep in-repo frames, truncate long messages, respect the diagnosis token budget.
- Every `file:line` in evidence must exist in the workspace; drop entries that do not.
- `confidence` is logged for analysis only. Do not branch on it; LLM self-reported confidence is not calibrated.

The diagnosis must include evidence.

Example:

```json
{
  "root_cause": "...",
  "evidence": [
    "stack trace points to service.py:42",
    "user_id is accessed without validation"
  ],
  "confidence": 0.86
}
```

---

# 16. Repair Budget

Default:

```yaml
max_iterations: 5
max_test_runs: 12
max_files_changed: 5
max_lines_changed: 150
max_tool_calls_per_iteration: 30
max_runtime_seconds: 1800
max_total_tokens: 1000000
max_cost: null          # per-task cap in budget_currency; set by the user, no fixed default
budget_currency: CNY
```

All values must be configurable. Check the budget before every node, not only inside the repair loop. A tool-using repair attempt commonly consumes hundreds of thousands of tokens, so do not "fix" budget overruns by shrinking context arbitrarily; report actual usage instead.

---

# 17. Infinite Loop Prevention

Stop when any of these holds, and record the matching `stop_reason` (exact definitions in 02「停止规则」):

| stop_reason | condition |
|---|---|
| `budget_*` | iterations / test runs / tokens / cost / wall time exhausted |
| `repeated_patch` | whitespace-normalized patch hash seen before |
| `repeated_failure` | identical failure-signature set two rounds in a row |
| `no_progress` | `made_progress` false two rounds in a row, or the current signature set already appeared in an earlier round (oscillation) |
| `sandbox_failure` | `SANDBOX_ERROR` or `ENV_ERROR` |
| `validation_loop` | three consecutive patch-validation failures |
| `diagnosis_give_up` | diagnosis strategy is `give_up` |

Also set LangGraph `recursion_limit = 20 + max_iterations * 6` as a last-resort guard.

Never implement an unbounded:

```python
while not success:
    repair()
```

loop.

---

# 18. Regression

Never consider a patch successful merely because the originally failing test passes.

At minimum:

```text
target test
→ affected tests
→ regression suite
```

The final success criterion should be:

```text
target PASS
AND
no new failures relative to TestBaseline
AND
review has no unresolved high-severity issue
```

- "Target" means the reproduction test if one was validated, otherwise the planner-selected existing tests.
- A regression is a test that passed on base and fails after the patch. Tests already failing on base do not count.
- Re-run new failures twice; inconsistent results mark the test flaky and exclude it, but list it in the report.
- If the full suite would exceed the remaining time budget, run affected tests plus a seeded 20% sample and mark `regression_mode=sampled`.
- Agent-side success is a self-assessment. Benchmark results come only from the official evaluation (see §20).

---

# 19. Evaluation

Required metrics (definitions in 02「指标定义」):

```text
File Recall@1/5/10, Function Recall@1/5/10
Resolved Rate
First Attempt Resolved
Repair Gain               = Resolved − First Attempt Resolved
Repair Success Rate       among tasks whose first attempt failed
Self-verified Precision   agent-claimed successes that evaluation confirms
Empty / Invalid Patch Rate
Iterations (mean / median / P90)
Tokens, cost, wall time per task; cost per resolved task
stop_reason distribution
```

- Denominator is always the total task count, including environment failures; report environment failures separately.
- Metric functions are pure and unit-tested.
- Save every attempt's patch so First Attempt Resolved can be evaluated.

Never fabricate benchmark numbers.

Every number in README or documentation must come from an actual recorded experiment.

---

# 20. Benchmark

Development order:

```text
synthetic fixtures (≥30, validated)
→ SWE-bench Lite: 1 task → dev subset (30) → test subset (50) → full 300
→ SWE-bench Verified (optional, V2)
```

- Synthetic fixtures follow the layout in 02「Benchmark 数据格式」. The `eval/` directory is never copied into the workspace.
- Every fixture must pass the validation script: target tests fail on base, pass with the gold patch, other tests unchanged.
- SWE-bench environments and judging use the official `swebench` package and harness. Do not maintain per-repo dependency setups by hand.
- Subset task lists are fixed files committed to the repo. Dev and test subsets never overlap.
- Estimate cost on 10 dev tasks before any batch run and write the estimate down.
- Batch runs must be resumable: skip instances that already have results.

Never use gold patches during inference (see §11.2).

Gold patches may only be used by the evaluation system after inference is complete.

---

# 21. Ablation

Support the configuration matrix in 02「消融」:

```text
A  no pre-injected retrieval, single attempt (tools still available)
B  + BM25
C  + semantic
D  + all channels + graph expansion
E  + iterative repair with raw logs
F  + structured failure diagnosis
G  + reproduction + review (full system)
```

- Each group changes exactly one config item relative to the previous one; configs live in `configs/ablation/`.
- Synthetic: 3 seeds per group. SWE-bench test subset: at least 1 run per group.
- Compare groups with paired bootstrap 95% CIs. If the interval contains 0, report "no significant difference observed".

Do not claim that a component improves performance until an actual experiment demonstrates it.

---

# 22. Testing

Every new feature must have tests.

Minimum:

```text
unit tests
integration tests
sandbox tests
agent graph tests
evaluation tests
```

For deterministic infrastructure, prefer deterministic fixtures over live LLM calls.

Use mocks/fakes only at boundaries where appropriate:

- `FakeLLM` — scripted responses; raises when the script runs out.
- `ReplayLLM` — replays a recorded `llm_calls.jsonl`; raises `ReplayMissError` instead of silently calling a real API.
- `FakeSandbox` — returns `ExecutionResult` by rule, so graph tests run without Docker.

pytest markers: `docker` (needs Docker), `llm` (needs a real API key, never in CI), `slow`. Default CI runs `-m "not docker and not llm"`.

Every routing branch and every `stop_reason` must have a graph test.

---

# 23. LLM Boundary

Keep the LLM provider behind an interface.

Example:

```python
class LLMProvider(Protocol):
    async def complete(self, request: LLMRequest) -> LLMResponse: ...
```

Do not scatter provider-specific SDK calls across the codebase.

- V1 providers: DeepSeek, Kimi (Moonshot), GLM (Zhipu), plus any other OpenAI-compatible endpoint. All go through one `OpenAICompatibleProvider`; they differ only in config (02「多 Provider 接入」).
- Never hardcode model IDs, base URLs, context windows or prices in code. They live in `configs/llm.yaml`; API keys are referenced by env var name only.
- Each agent role (planner, coder, diagnoser, …) maps to a model alias, so roles can use different models.
- Handle capability differences (no native tool calling, no JSON mode, ignored temperature, reasoning content) through capability flags in the model config, never through `if provider == ...` branches in business logic.
- Reasoning / thinking content is logged but never fed back into the conversation or stored in `RepairState`.

- Structured output: JSON Schema → parse → Pydantic validate → retry with the validation error (max 2) → `StructuredOutputError`. Never store unvalidated free text as internal state.
- Every call records input/output tokens, cost, latency and `purpose`, and is appended to `llm_calls.jsonl` through `RecordingLLM`.
- Retry 429 / 5xx with exponential backoff (max 3). Retries count toward cost, not toward repair iterations.

---

# 24. Configuration

Use environment variables / Pydantic Settings.

Do not hardcode:

- API keys;
- model names;
- database passwords;
- credentials;
- production URLs.

Provide:

```text
.env.example
```

Never commit:

```text
.env
API keys
tokens
secrets
```

---

# 25. Observability

Every Agent Node must emit an event containing:

```text
run_id
task_id
node
timestamp
duration
token usage
tool calls
status
error
```

Event types: `node_start`, `node_end`, `llm_call`, `tool_call`, `sandbox_run`, `budget_check`, `stop`. Each event has a monotonically increasing `seq` per run. Schema in 02「事件」.

Do not log secrets. Pass every event payload and saved config through a redaction function before writing.

---

# 26. Error Handling

Errors must be classified.

Do not use:

```python
except Exception:
    pass
```

unless there is an explicit documented reason.

Prefer:

```python
except SpecificError as exc:
    logger.exception(...)
    ...
```

---

# 27. Database

Use SQLAlchemy 2. Default to SQLite; PostgreSQL is selected via `DATABASE_URL` and must work with the same models.

Redis is optional (queue / cache / locks for the API worker). The CLI and benchmark runner must work without it.

Do not make Redis the sole source of truth for completed experiments. Completed runs live in the database plus `artifacts/runs/<run_id>/` (layout in 02「Artifact 目录」).

Every run records `code_version` (git commit + dirty flag) and a config hash so results are traceable.

---

# 28. Git

Every repair run uses an isolated workspace under `runs/<run_id>/workspace/`, created from a shared read-only clone cache. User-provided local paths are copied, never edited in place. Reverting to base only touches the workspace.

Use the `git` CLI via subprocess with argument lists (no shell strings). Git history features may only read ancestors of the base commit.

Preserve:

```text
base commit
patch history
final diff
test results
```

Never modify the user's original working tree unexpectedly.

---

# 29. API

Minimum:

```text
POST /api/v1/tasks
GET  /api/v1/tasks/{task_id}
GET  /api/v1/runs/{run_id}/events      SSE, supports ?after_seq=
GET  /api/v1/runs/{run_id}/patch
GET  /api/v1/runs/{run_id}/report
POST /api/v1/runs/{run_id}/cancel
GET  /api/v1/benchmarks/{bench_id}
```

Use Pydantic schemas.

- Require `X-API-Key`; refuse to start if no key is configured.
- Bind to `127.0.0.1` by default.
- Accept only `https://` repo URLs from an allowlist; reject `file://` and local paths over the API.
- Agent runs execute in a background worker, never inside the request handler.

---

# 30. Code Quality

Use:

```text
ruff
mypy
pytest
```

Code should favor:

- explicit types;
- small functions;
- dependency injection;
- pure functions for scoring/parsing;
- clear interfaces;
- structured logging.

Avoid unnecessary abstractions.

---

# 31. Implementation Sequence

Follow this order unless a concrete dependency requires otherwise. Phase numbers, tasks and acceptance criteria are defined in `04-分阶段开发任务清单.md`; that file is authoritative.

```text
Phase 0   Project skeleton + LLM layer
Phase 1   Repository Index
Phase 2   Retrieval
Phase 3   Issue Analysis + Localization
Phase 4   Sandbox + environment build        (depends only on Phase 0)
Phase 5   Planning + Coding + Patch Validation   → M1 minimal loop
Phase 6   Issue Reproduction
Phase 7   Failure Diagnosis
Phase 8   Iterative Repair (full LangGraph)
Phase 9   Regression + Review + Run Report    → M2 synthetic loop
Phase 10  Synthetic Benchmark
Phase 11  SWE-bench Lite                      → M3
Phase 12  Ablation + experiment report        → M4
Phase 13  API + Dashboard
Phase 14  Packaging
```

Phases 0–9 are the core loop and are never cut. If the schedule slips, cut in this order: Dashboard → static HTML report; SWE-bench full run → fixed 50-task subset; ablation → groups A / D / G only.

---

# 32. Phase Completion Rule

A phase is complete only when:

```text
implementation
+
tests                 uv run pytest -m "not docker and not llm"
+
docker tests          uv run pytest -m docker   (when the phase touches the sandbox)
+
lint                  uv run ruff check .
+
type check            uv run mypy src
+
documentation
+
phase acceptance      the checks listed for this phase in 04
```

all pass. Acceptance steps that require a real LLM are run via scripts in `scripts/` and their outputs saved under `artifacts/`; report the actual numbers, whatever they are.

If any of these fail, do not report the phase as complete.

---

# 33. Coding Agent Working Protocol

Before coding:

```text
1. Inspect repository.
2. Read relevant docs.
3. Identify current phase.
4. Identify impacted files.
5. Explain implementation plan internally.
```

During coding:

```text
1. Make minimal changes.
2. Preserve existing interfaces.
3. Add tests alongside code.
4. Avoid unrelated refactors.
```

After coding:

```text
1. Run tests.
2. Run ruff.
3. Run mypy.
4. Fix failures.
5. Inspect git diff.
6. Update docs.
```

Final response must contain:

```text
Implemented:
- ...

Tests:
- pytest: PASS/FAIL (N passed, M skipped)
- pytest -m docker: PASS/FAIL/NOT RUN (reason)
- ruff: PASS/FAIL
- mypy: PASS/FAIL

Acceptance (from 04):
- <each check>: PASS/FAIL/NOT RUN, with artifact path if applicable

Files changed:
- ...

Known limitations:
- ...

Next recommended phase:
- ...
```

Never claim commands passed unless they were actually executed.

---

# 34. Do Not Over-engineer

Do not add:

- Kubernetes;
- Kafka;
- microservices;
- distributed vector infrastructure;
- complex event sourcing;
- custom model training;
- mandatory PostgreSQL / Redis for the CLI path;
- a vector database server (numpy brute force is enough at single-repo scale);
- a frontend before Phase 13;

until the single-machine pipeline works end-to-end.

The primary objective is:

> one repository + one issue → reliable autonomous repair loop.

---

# 35. Definition of Success

The project is successful when the system can demonstrate:

```text
real repository
+
real issue
+
repository-aware localization
+
patch generation
+
real execution
+
failure diagnosis
+
iterative repair
+
regression testing against a base-commit baseline
+
auditable, replayable trace
+
benchmark performance measured by the official harness under information isolation
+
ablation showing what each component contributes, with confidence intervals
```

A visually impressive dashboard without this pipeline is not considered successful. Honest negative results with failure analysis are an acceptable outcome; inflated or unverifiable numbers are not.
