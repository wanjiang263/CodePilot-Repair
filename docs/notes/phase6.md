# Phase 6：Issue Reproduction 开发记录

日期：2026-10-14
分支：`codex/phase6-reproduction`（基线 `f093294`，即 Phase 5 HEAD）
本地提交：`40373bb` → `b7777a6` → `662d6fc` → `a080db8` → `984d084` → `d732209` → `9d22c40`

## 实现范围（D-01～D-09）

```text
Issue + IssueAnalysis + LocalizationResult
  → ReproductionAgent（结构化输出 ReproTestDraft）
  → 静态筛查伪复现 + 定位关联校验
  → WorkspaceManager 独立 base Workspace + Sandbox 执行指定 pytest NodeID
  → ExecutionResult + JUnit 结构化结局判定
      ├─ 有效 → 落盘 tests/_codepilot_repro/，产出 target_tests
      ├─ 无效 → 带脱敏证据重试（1 次初始 + 最多 3 次重试）
      └─ 环境故障 → 立即短路为 env_error，不消耗 LLM 预算
  → 重试耗尽：status=failed + 降级为仓库已有测试，不中断后续修复
  → 交付 Git Diff 剔除 tests/_codepilot_repro/
```

| ID | 落点 | 状态 |
|---|---|---|
| D-01 | `schemas/reproduction.py`（权威定义）+ `agents/schemas.py` 再导出 | IMPLEMENTED |
| D-02 | `agents/reproducer.py`（`ReproTestDraft`、`screen_reproduction_test`、`ReproductionAgent`） | IMPLEMENTED |
| D-03 | `agents/reproducer.py`（`BaseWorkspaceValidator`、`classify_base_execution`、`WorkspaceManagerBaseProvider`） | IMPLEMENTED |
| D-04 | `agents/reproducer.py`（重试循环、`ReproductionOutcome.target_tests` 降级） | IMPLEMENTED |
| D-05 | `tools/base.py`（`WorkspacePathPolicy(repro_write=…)`）+ `patch/validator.py` 一致性 | IMPLEMENTED |
| D-06 | `patch/diff.py`（`generate_git_diff` 默认剔除 + `excluded_files`） | IMPLEMENTED |
| D-07 | `patch/diff.py` + `patch/validator.py`（统计隔离、evidence 记录） | IMPLEMENTED |
| D-08 | `scripts/phase6_repro_stats.py` + `codepilot.eval`（base FAIL / gold PASS 双条件） | IMPLEMENTED |
| D-09 | `eval/metrics.py`（`self_verified_precision` 纯函数） | IMPLEMENTED |

## 关键设计决定

- **契约仍只有一份**：`ReproductionResult` 沿用 Phase 0 的 `schemas/reproduction.py`，按 docs/05 入口由 `agents/schemas.py` 再导出；新增 `kind` / `history` 两个**带默认值**的可选审计字段，`RepairState.reproduction` 的读写与 round-trip 不受影响（`tests/unit/schemas/test_state_roundtrip.py` 已随之加强，未放宽断言）。
- **计数规则显式化**：docs/05 D-04 的"最多重试 3 次"实现为 **1 次初始尝试 + 最多 3 次重试**，`MAX_REPRO_ATTEMPTS = 4`，`retries = attempts - 1`；schema 强制 `attempts == len(history)` 且 index 自 1 连续，因此尝试次数无法被手写虚构。
- **`ENV_ERROR` 是独立状态**：环境/沙箱/工作树故障不能被当成"复现失败"重复烧 LLM 预算，必须由调用方按既有 Sandbox 故障路由处理。
- **失败类型来自 JUnit 结构化字段**：`TestCaseResult` 新增 `failure_type`，由 `sandbox/junit.py` 按「`type` 属性 → traceback 末行 `file:line: ExcName` → message 前缀」三级提取。实测 pytest 的 xunit2 格式对 `<failure>` **不写** `type` 属性，因此 traceback 末行是主来源；stdout 不参与判定。
- **`Failed: DID NOT RAISE X` 属断言族**：`pytest.raises` 未抛期望异常时 pytest 报 `Failed` 而非 `AssertionError`。本轮按 03 §11.1 的口径将其计入"断言类有效"，只有当 X 确实出现在 Issue 文本里才升级为"Issue 点名异常"。这是一个刻意放宽，配套的负向控制是无条件 `pytest.fail()`（静态拒绝）与"未声明异常类型"（判无效）。
- **"base 上有效失败" ≠ "问题已复现"**：ReproductionAgent 只声明前者；后者由评测侧的双条件确认，写进 `docs/02` §10.5 与本记录。

## 安全与隔离

| 边界 | 实现 | 证据 |
|---|---|---|
| 复现目录写入 | `WorkspacePathPolicy(repro_write=True)` 是可信构造参数；无凭证时目录不可写；有凭证仅接受 `tests/_codepilot_repro/test_*.py` 一层，`conftest.py`/`__init__.py`/嵌套/大小写变体/`..`/绝对/UNC 全拒；realpath 后校验父目录身份以挡 symlink 换出 | `tests/unit/patch/test_phase6_repro_isolation.py`（14 条伪造与逃逸参数化）、`tests/unit/agents/test_reproducer_base_validation.py` |
| Coding Agent 不获此权 | Coder 的 `create_file`/`edit_file` 使用未授权策略，返回 `forbidden_path`；`allow_test_edits` 是 `ValidatorConfig` 的可信参数，与写入凭证是两套对象 | `test_model_cannot_widen_permissions_through_allow_test_edits` |
| 交付 Patch 剔除 | 在 `generate_git_diff` 单点实现，四种补丁来源同时生效；`excluded_files` 可审计；`excluded_directories=()` 仍可取原始工作树 diff | `test_delivered_patch_keeps_source_changes_and_drops_reproduction_files`、`test_unified_diff_sourced_changes_are_filtered_the_same_way`、`test_no_exclusions_means_the_raw_workspace_diff_is_available` |
| 统计隔离 | 复现文件不进 `files_changed` / `lines_added`；`max_lines_changed=10` 下 160 行复现文件不影响判定；evidence 记录剔除项；只有复现文件时按空补丁拒绝 | `test_patch_statistics_ignore_reproduction_files` |
| 不动用户仓库 | 忽略语义在 diff 层实现，`.gitignore` 原样；工作树仍 `git status` 到复现文件；原始 fixture 运行前后 `git status --porcelain` 为空、HEAD 与文件内容不变 | `test_user_gitignore_is_not_rewritten_and_other_files_stay_tracked`、两条真实 Docker E2E |
| Gold Patch 隔离 | 推理侧只用 `RetrievalCase.inference(dataset_root)` 投影（`task_id/repo/base_commit/issue`）；gold 只在 `GoldReproEvaluator` 里按 sha256 校验后 `git apply`；`build_inference_inputs` 主动扫描 `retrieval_gold/gold_patch/fail_to_pass/pass_to_pass/reference.patch` 标记并抛错 | `scripts/phase6_repro_stats.py --verify-isolation` 12/12 通过；`test_isolation_guard_rejects_a_leaking_inference_projection`；`test_no_candidate_borrows_a_line_from_the_gold_patch` |

## 定向验证

| 范围 | 命令 | 结果 |
|---|---|---|
| D-01 契约 | `pytest tests/unit/schemas/test_reproduction_schema.py` | 31 passed |
| D-02 生成与筛查 | `pytest tests/unit/agents/test_reproducer_screening.py` | 29 passed（含一次真实 `pytest --collect-only` 子进程证明可收集） |
| D-03 base 校验 | `pytest tests/unit/agents/test_reproducer_base_validation.py` | 22 passed |
| D-04 重试降级 | `pytest tests/unit/agents/test_reproducer_agent.py` | 19 passed |
| D-05～D-07 | `pytest tests/unit/patch/test_phase6_repro_isolation.py` | 30 passed，1 条 symlink 用例在本机 skip（见 DA-002） |
| D-08 离线部分 | `pytest tests/unit/test_phase6_repro_stats.py` | 37 passed |
| D-09 指标 | `pytest tests/unit/eval` | 24 passed |
| FakeLLM + FakeSandbox E2E | `pytest tests/graph/test_phase6_reproduction_e2e.py -m "not docker"` | **9 passed / 2 docker deselected** |
| Phase 6 合计（离线） | 上述定向集合 | **388 passed / 2 docker deselected**，exit 0 |

FakeSandbox 的每个结果都带 `execution_backend=fake` 与 `container_verified=False`，`RunReport.simulated_sandbox` 据此判定，不与真实执行混淆。

## 真实 Docker 验收

环境：Docker Desktop Engine 29.8.2 / Linux daemon（`docker info` 实测），复用 Phase 4 已确认的镜像基座。

```text
pytest tests/graph/test_phase6_reproduction_e2e.py -m docker -q
→ 2 passed, 9 deselected in 16.67s，退出码 0
```

- **有效复现**：真实容器里 `normalize(3) == 3` 以 `AssertionError: assert 2 == 3` 失败，`exit_code=1`，`failure_type=AssertionError`，`attempts=1`，状态 `reproduced`；证据 `reproduction/attempt-01-outcome.json` 记录 `execution_backend=docker`、`container_verified=true`；交付 diff 为空且 `excluded_files=[tests/_codepilot_repro/test_repro_normalize.py]`；原始 git fixture `git status --porcelain` 为空。
- **无效复现**：一份"触达定位文件、但把缺陷行为当成正确期望"的测试在 base 上为绿，容器给出 `PASS`，Agent 判 `invalid_pass` → 状态 `failed`、`attempts=1`（`max_retries=0`）、降级到 `tests/test_core.py`，修复工作树里不留复现目录。
- **一次真实环境故障被正确分流**：首轮每条场景各自 `build_env`（repository 身份随 tmp_path 变化）时，第二次 pip 下载在宿主机代理下撞上哈希校验失败，Agent 返回 `status=env_error / kind=env_error / attempts=1`，**没有**把它当成复现失败去重试。修复方式是 session 级共享一份镜像（`d732209`），故障分流路径本身保持原样。
- 未重跑 Phase 4 的 24 项 Docker 验收：本轮未改动 `DockerSandbox` 核心（只改了 `run_git` 的 stdin 换行与 JUnit 解析），Phase 4 的隔离与清理断言不在受影响面上。

## Synthetic 复现成功率（D-08）

```text
python scripts/phase6_repro_stats.py
→ 退出码 0，12 条任务全部报告，70.12s wall
产物：artifacts/phase6/{results.jsonl,metrics.json,summary.md}
```

| 指标 | 分子 / 分母 | 值 |
|---|---|---|
| 生成候选复现测试 | 12 / 12 任务 | generation_rate = 1.0 |
| base 上有效失败 | 12 / 12 已生成 | base_valid_failure_rate = 1.0 |
| Gold Patch 后通过 | 12 / 12 有效失败 | gold_pass_rate = 1.0 |
| **复现成功（双条件）** | 12 / 12 任务 | reproduction_success_rate = 1.0 |
| 平均尝试次数 | attempts 合计 12 / 12 | 1.0（max 1） |
| 重试耗尽 | 0 / 12 | — |
| 环境错误 | 0 / 12 | — |
| 降级为已有测试 | 0 / 12 | — |
| 结局分布 | 按尝试计 | `valid_assertion` 10、`valid_issue_exception` 2 |

真实样本证据（不是聚合数的修辞）：`01-expiry-boundary` 得到 `AssertionError: assert None == 'demo'`；`02-malformed-expiry` 得到 `ValueError: invalid literal for int() with base 10: 'not-an-int'`，被归类为 `valid_issue_exception`，因为 Issue 文本确实点名 ValueError；`03-zero-ttl` 得到 `Failed: DID NOT RAISE ValueError`，按断言族归类为 `valid_assertion`。

**这批数字的边界必须写清楚**：数据集是 12 个既有 synthetic-development 夹具（未新增、未虚构）；候选复现测试由本轮开发者按 Issue 文本与 base 源码**手写进 FakeLLM 脚本**，经正常 Provider 接口投递。因此 12/12 衡量的是"base FAIL / gold PASS 这套判别器是否可信、链路是否真的能生成—执行—判定—剔除"，**不是**任何真实模型的复现能力，也不构成 Phase 10 的合成 Benchmark（未提前建设）。配套的两条负向控制（伪复现静态拒绝、base-green 判 invalid_pass）说明这个 1.0 不是"什么都算成功"的空判据。真实模型证据登记为 **DA-008 NOT RUN**。

## Self-verified Precision（D-09）

`codepilot.eval.metrics.self_verified_precision` 是纯函数（有单测扫描 import 证明不依赖 docker/os/文件）。口径：分母 = Agent 自认成功 **且** 评测给出结论的任务数；`evaluation_passed is None`（环境失败）的自认成功被排除并单独计数；无自认成功 → `value=None`，绝不返回 1.0。单测含一个 10 任务手算表（分子 3 / 分母 5 = 0.6）与逐项口径断言。24 passed。它不是 Resolved Rate，Phase 10/11 直接复用同一入参结构。

## 过程中发现并修复的缺陷

| 级别 | 缺陷 | 处理 |
|---|---|---|
| P1 | `run_git` 以 `text=True` 写 stdin，Windows 把补丁的 `\n` 翻成 CRLF，而 workspace 强制 `core.autocrlf=false` 是 LF → `git apply` 报 "patch does not apply"，Unified Diff 来源在默认开发平台上是坏的 | `gitcmd.py` 把 stdin `reconfigure(newline="\n")`（`b7777a6`）；配套夹具改为显式 LF（`a080db8`），此前 CRLF 夹具与该 bug 恰好互相抵消，等于没测到真实形态 |
| P2 | D-08 首轮在第 8 题崩溃：脚本只喂一条候选，而 harness 把 `LocalizationResult` 允许的 3 文件截到 2，`models.py`（BM25 第 3 名）被截掉 → 合法候选被判"与定位无关" | 取满契约上限 + FakeLLM 给 `default` 使重试按预算自然耗尽（`9d22c40`）。**这是评测夹具的问题，不是复现判定的问题**；排序深度归 DA-005 的定位召回评估范围 |

## 与 Phase 5 的兼容

`graph/minimal.py` 与 CLI 未被改写：复现能力是独立入口（`ReproductionAgent` + `BaseWorkspaceValidator`），未来的 REPRODUCE 节点只需把 `ReproductionOutcome.result` 写进 `RepairState.reproduction`。回归护栏 `test_phase5_single_patch_loop_still_works_with_the_reproduction_primitives` 与既有 `tests/graph/test_minimal_e2e.py`（9 passed）均通过，Phase 5 单轮修复语义与交付补丁不变。

## 最终质量门禁

四条命令均在最后一笔代码改动（`9d22c40`）之后、于同一工作树状态上执行；文档改动不参与门禁。

| 命令 | 退出码 | 结果 |
|---|---|---|
| `ruff check .` | 0 | All checks passed |
| `ruff format --check .` | 0 | 252 files already formatted |
| `mypy src`（strict） | 0 | no issues found in **127** source files（Phase 5 为 124，本轮 +reproducer、+eval/metrics、+eval/__init__） |
| `pytest -m "not docker and not llm" --cov --cov-report=term-missing` | 0 | **2107 passed / 0 failed / 1 skipped / 28 deselected**，覆盖率 **93.81%**，1063.31s（17:43） |

对照 Phase 5 基线（1906 passed、93.63%）：净增 **201** 条测试，覆盖率 **+0.18pp**；`fail_under=80` 未下调，没有弱化断言，也没有为绕过失败新增 skip。

唯一的 skip 是既有项：`tests/unit/retrieval/test_embeddings.py` 需要 `CODEPILOT_TEST_EMBEDDING_MODEL` 指向预装模型，与本轮无关。

**28 项 deselected 全部是 `docker` 标记用例**，按文件计：`tests/docker/*` 24 项（Phase 4 运行时验收）、`tests/integration/test_docker_phase1.py` 1 项、`tests/graph/test_minimal_e2e.py` 1 项（Phase 5 E2E）、`tests/graph/test_phase6_reproduction_e2e.py` 2 项（本轮新增）。`pytest -m llm --collect-only` 实测收集到 **0** 条，即当前套件里没有 `@pytest.mark.llm` 用例——Phase 5 记录里"26 deselected = 25 docker + 1 llm"的说法与今天的套件不一致，此处以实测为准，并把真实模型验收继续记为 NOT RUN（P-24、DA-008）。这些 deselected 一律不得计作通过。

真实 Docker 侧单独执行，退出码 0（见上一节）；D-08 评测脚本单独执行，退出码 0。

## 遗留与延期

- **P-24 真实 LLM 修复验收**：继续 **NOT RUN**（本轮未调用任何付费模型）。
- **DA-001～DA-006**：原状态保留。
- **DA-002 / DA-003**：本轮新增的 symlink 逃逸用例在无符号链接权限的 Windows 主机上 skip，缺口仍归 Linux CI 验收，未关闭。
- **DA-007**：保持已关闭。
- **DA-008（新增）**：真实模型的 Issue Reproduction 验收 **NOT RUN**。
- **DA-009（新增）**：Coding Agent 仍可**新增**测试文件进入交付补丁（Phase 5 既有行为，本轮未扩大也未修复），建议在 Phase 9 Review 定稿。
- 未发现 P0 阻断问题。
