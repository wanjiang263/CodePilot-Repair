# 任务书勾选同步记录（2026-11-12）

基线：`b8e1fa91e0633aa0fa59aff5fcfd3e74b58b4aed`（`main`）。

## 1. 语义

本记录只同步 [04-分阶段开发任务清单](../04-分阶段开发任务清单.md) 的**任务框**。按 Phase 4 批注已确立的语义，任务框表示「代码与对应单测已落地」，**不等同于阶段验收**。阶段验收仍按 `03-AGENTS.md` §32 单独判断；本次没有把任何 `NOT RUN` 的运行时验收写成 PASS，也没有把任何阶段标为完成。

## 2. 本次补齐的勾选

| 范围 | 勾选数 | 主要依据 |
|---|---:|---|
| Phase 0 | 18 | docs/05 §4.3 与 v1.1：Phase 0 完成记录（368 passed、ruff/format/mypy 全绿、`codepilot --help` exit 0、round-trip、录制→回放逐字节一致、结构化输出重试）；当前 `src/` 中各组件与测试均在 |
| Phase 1 | 12 | docs/05 v1.4–v1.7、R-18 / R-21 / R-23；[phase1.md](phase1.md)；`tests/integration/test_cli_index.py`、`test_index_golden.py`、`test_incremental.py` |
| Phase 3 | 7 | docs/05 §7 C-01～C-13、v1.11 / v1.12；[phase3-issue-analysis.md](phase3-issue-analysis.md)、[phase3-localization.md](phase3-localization.md) |
| Phase 11 | 6 | docs/05 v1.19：E-11～E-16、E-19 工程实现与离线单测落地；`src/codepilot/eval/swebench*.py`、`src/codepilot/sandbox/swebench_env.py` 与 `tests/eval/test_swebench_*.py`、`tests/sandbox/test_swebench_env.py` |
| **合计** | **43** | |

Phase 11 保留未勾的 2 项是 E-17（1 题 / 50 题 / 300 题真实跑批）与 E-20（真实费用校准）；官方 Harness 未成功执行，M3 未达成。

## 3. 本次实际执行的命令与结果

| 命令 | 结果 |
|---|---|
| `uv run codepilot --help` | exit 0（Phase 0 验收命令之一） |
| `uv run codepilot index tests/fixtures/repos/mini_auth` | **exit 1**：该 fixture 不是 git 仓库（没有 `.git`）。`--mode copy` 同样 exit 1。等价真实路径由 `tests/integration/test_cli_index.py` 在 `tmp_path` 现建 git 仓库覆盖（docs/05 R-18），不是索引功能缺失 |
| `uv run pytest ...` / `uv run python -m pytest ...` | **NOT RUN**：本机 `.venv` 缺 `dev` 额外依赖；`uv sync --frozen --extra dev` 因网络拒绝失败（`tcp connect error` / `os error 10061`），`uv pip install --offline pytest` 缓存中没有包 |
| `grep` 抽查 `RepairState` / `RepairBudget` / `ExecutionResult` / `FakeLLM` / `RecordingLLM` / `ReplayLLM` / `WorkspaceManager`（`src/codepilot/repo/workspace.py:241`）/ `RepositoryScanner` / `IssueAnalyzer` / `ContextBuilder` / `LocalizationResult` / `PythonParser` | 均命中 |
| `pyproject.toml` marker 检查 | `docker` / `llm` / `slow` 三个 marker 均在（L290-293） |
| `tests/fixtures/repos/` 清点 | `mini_auth`、`mini_calc`、`mini_pkg` 等 6 个 fixture 目录 |

测试证据以基线提交的 CI 为准：[run 42480322292](https://github.com/wanjiang263/CodePilot-Repair/actions/runs/42480322292)，`b8e1fa9`，pytest 4193 passed / 9 skipped / 50 deselected，六个 job 全部 success。本次是纯文档提交，未重跑该 CI 结果。

## 4. 未勾选项与原因（同步后共 23 项）

| 范围 | 未勾数 | 原因 |
|---|---:|---|
| Phase 11 | 2 | E-17 真实 1 / 50 / 300 题跑批、E-20 真实费用校准：`NOT RUN`（需真实 LLM / Docker / 费用授权） |
| Phase 12 | 6 | M4 消融未执行；`configs/ablation/` 与 `scripts/run_ablation.py` 未落地，结果与实验报告不存在 |
| Phase 14 | 3 | Bedrock 真实 AWS、真实付费 Provider、demo 录屏：`NOT RUN` / `BLOCKED` |
| Phase 15 | 4 | 最小真实推理、Live M2 E2E、Anthropic / Bedrock 真实调用、M3 正式批跑：`NOT RUN` |
| Phase 18 / 18E | 4 | 账本冻结、test50 / M4、pytest P2 `NOT VERIFIED`、后续付费派发需书面授权 |
| Phase 19 | 1 | 定向测试、M3 正式批跑、M4 / test50 / test300：`NOT RUN` |
| Phase 20 | 1 | test50 / test300、M4、全仓 pytest、全量 Docker suite：`NOT RUN` |
| Phase 21 | 2 | 官方 Harness 0 次；进一步付费派发需书面授权 |

## 5. 保留的边界

- 本次勾选**不表示** Phase 0 / 1 / 3 / 11 的运行时验收全部完成；Phase 3 的 C-14、Phase 11 的 E-17 / E-20、Phase 0 的 L-18 等 `NOT RUN` 项按原样保留。
- `uv run codepilot index tests/fixtures/repos/mini_auth` 在干净检出的失败是 fixture 非 git 仓库导致的命令口径偏差，不是本次新发现的实现缺陷；已在 docs/04 Phase 1 勾选说明中标注。
- 本记录不修改历史阶段记录，也不关闭任何 Deferred Acceptance 台账条目。
