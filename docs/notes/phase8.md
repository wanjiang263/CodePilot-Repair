# Phase 8：Iterative Repair（多轮自主修复，G-01～G-24）

日期：2026-10-14/15。开发分支：`codex/phase8-iterative-repair`。
基线：`df9807940c45fbf72865a59459eeddfde4ba3847`（Phase 7 收口）。

目标不是"给 Phase 5 的 solve 加一层 for 循环"，而是把 Phase 5/6/7 组装成
**可执行、可恢复、有预算边界、可解释**的真实多轮修复状态机。

---

## 1. 交付状态与证据

| ID | 实现文件（src/codepilot 下） | 主要测试证据 | 状态 |
|---|---|---|---|
| G-01 | graph/nodes.py；graph/builder.py | test_graph_structure：16 节点集合、条件边、每个节点是 `async def node(state, deps)`；test_path_* 全部跑真图 | IMPLEMENTED |
| G-02 | graph/deps.py | test_deps：run/task/base_commit 身份绑定、计数器对齐、Usage 原地恢复、并行 run 不共享计数、close 不删 Workspace | IMPLEMENTED |
| G-03 | graph/routing.py | test_routing：六态执行、终态短路、confidence 四值同解、OOM 走终止边、无目标测试进回归边界 | IMPLEMENTED |
| G-04 | graph/budget.py | test_budget：六类各自触发、`>= 上限` 即耗尽、未设 max_cost 不误判、迭代 off-by-one、负数上限报错 | IMPLEMENTED |
| G-05 | graph/nodes.py `_budget_guard` | test_path_budget（9 项）+ test_repair_strategies：耗尽时不调 LLM、不动 Workspace、finalize 仍可出报告 | IMPLEMENTED |
| G-06 | graph/progress.py；schemas/progress.py | test_progress：三种进展、无基线首轮、日志顺序无关、未测量回归不参与比较 | IMPLEMENTED |
| G-07 | graph/progress.py | test_progress 参数化 A→B→A / A→B→C→A / 四轮周期，并给出回到的是哪一轮 | IMPLEMENTED |
| G-08 | graph/stop_rules.py | test_stop_rules（24 项）：§15.5 全表 + `canonical_patch` 负控制；工具调用上限映射到 BUDGET_TOOL_CALLS | IMPLEMENTED |
| G-09 | graph/nodes.py `repair_patch` | test_repair_strategies（12 项）：refine 保留改动、revert 逐字恢复文件并重规划、expand_scope 只加真实 symbol | IMPLEMENTED |
| G-10 | graph/repair_policy.py | test_repair_policy：refine/refine/revert 完整序列；confidence 0.0～1.0 都不能豁免 | IMPLEMENTED |
| G-11 | graph/nodes.py | test_attempt_history（9 项）：只追加、失败轮保留、JSON round-trip、重放不重复追加、秘密被脱敏 | IMPLEMENTED |
| G-12 | graph/checkpoint.py | test_resume：checkpoint 读回同一状态、thread_id=run_id、多 run 隔离、manifest 记录 schema/code_version | IMPLEMENTED |
| G-13 | graph/checkpoint.py；tests/graph/_phase8_resume_worker.py | test_resume：**子进程 `os._exit(7)` 崩溃后新进程恢复**，不重放已完成节点、不重复计费、不重复追加 Attempt | IMPLEMENTED |
| G-14 | graph/builder.py | test_graph_structure：公式参数化；小上限真的抛 GraphRecursionError 并被保守地上报 | IMPLEMENTED |
| G-15 | tests/graph/test_path_first_pass.py | 一次通过 + Phase 9 不伪造 + 预算口径 + Artifact 现场 | PASS |
| G-16 | tests/graph/test_path_second_pass.py | 第 2 轮的 search 块只能在第 1 轮落盘后存在 | PASS |
| G-17 | tests/graph/test_path_repeated_failure.py | 含 DA-010 负控制：签名相同但失败身份不同**不**停止 | PASS |
| G-18 | tests/graph/test_path_repeated_patch.py | 含"仅缩进不同不是同一补丁"的图级负控制 | PASS |
| G-19 | tests/graph/test_path_no_progress.py | 越修越差 + A→B→A 振荡两种形状 | PASS |
| G-20 | tests/graph/test_path_budget.py | 六类预算各自触发，停止后副作用计数冻结 | PASS |
| G-21 | tests/graph/test_path_sandbox_error.py | ENV/SANDBOX/OOM 直接 finalize；COLLECTION/TIMEOUT 仍进诊断（对照组） | PASS |
| G-22 | tests/graph/test_path_validation_retry.py | 无效补丁从不执行；连续三次触发 validation_loop | PASS |
| G-23 | tests/graph/test_resume.py | 独立进程崩溃 + 恢复；Workspace/Artifact/补丁身份一致性核对 | PASS |
| G-24 | tests/graph/test_termination.py | 12 种形状参数化，全部终态且步数 < recursion_limit | PASS |

集成 A（Phase 5→7→8）与 B（Phase 6 复现降级）见
[tests/graph/test_phase8_integration.py](../../tests/graph/test_phase8_integration.py)，
集成 C（FakeLLM + 真实 DockerSandbox）见
[tests/graph/test_phase8_docker_e2e.py](../../tests/graph/test_phase8_docker_e2e.py)。

---

## 2. 节点与路由（02 §15.2 的落地形状）

```text
START → understand_issue → retrieve → localize → analyze_impact
      → build_env → reproduce → plan → generate_patch → validate_patch
validate_patch  ─ invalid → check_budget
                ─ valid & 有目标测试 → run_target_tests
                ─ valid & 无目标测试 → run_regression
run_target_tests ─ PASS → run_regression
                 ─ FAIL/TIMEOUT/COLLECTION_ERROR → diagnose
                 ─ ENV_ERROR/SANDBOX_ERROR → finalize
run_regression  ─ 有新增失败 → diagnose ；否则 → review
review          ─ high 且未复审 → check_budget ；否则 → finalize
diagnose        → check_budget
check_budget    ─ 停止 → finalize ；否则 → repair_patch
repair_patch    → validate_patch          finalize → END
```

除 `understand_issue` 外的每条边都由 `graph/routing.py` 的纯函数决定，图里不内联
任何判断。任何节点把 `state.status` 写成非 `running`，下一条边一律指向
`finalize`——这让"错误不能被转换成假成功"只有一条实现路径。

**Phase 9 边界**：`run_regression` / `review` 是真实节点，但本轮没有
`RegressionRunner` / `PatchReviewer` 实现（G-25～G-37）。两个节点在 `deps` 未注入
实现时如实记录 `DEFERRED:phase9-not-implemented`，`state.regression` /
`state.review` 保持 `None`，**不产出永远 PASS 的占位对象**。目标测试通过后的成功
范围是 `provisionally_verified`，报告与 `report_artifacts` 都写明回归与复审未执行。

---

## 3. 本轮实际改掉的两处判断口径

这两处都不是"补测试"，而是实现本身改了口径，需要留档。

### 3.1 Patch hash 不"去掉所有空白"

02 §15.5 写"patch hash 对 diff 去掉空白差异后计算"。**按字面实现是错的**：
Python 的缩进是语法，三引号字符串**行尾的空格也是数据**。若把 diff 的全部空白
抹掉，"换个缩进又交同一份坏代码"和"改了字符串里的空格"都会得到同一个哈希，
`repeated_patch` 就会在错误的时间点停止修复。

`canonical_patch` 改成只归一化**不含内容的 git 元数据**：丢 `index <sha>..<sha>`、
`--- a/`、`+++ b/`（文件身份已在 `diff --git` 行里），把 `\ No newline at end of
file` 转成显式 `!NOEOL!` 标记（它代表真实内容差异，不能当噪声丢），统一 CRLF→LF；
正文行（含缩进与行尾空格）逐字保留。

代价写进文档：只差一个行尾空格的两份补丁会被判为不同补丁，多花一轮预算。方向是
安全的——**误判重复会提前停止修复（漏修），漏判重复只是多修一轮**。
`tests/unit/graph/test_stop_rules.py` 用两条负控制钉住：重排缩进、字符串内空格，
都必须得到不同哈希。

同时 docs/05 G-08 的"8 种 stop_reason"是表格行数口径，按 02 §15.5 逐项展开是
11 个 `StopReason`（5 个 `budget_*` + 6 个）；权威枚举里另有 `budget_tool_calls`
等 5 个不属于该表的成员，也已显式接线，不静默忽略。

### 3.2 振荡判据与重复失败共用同一套身份核验

第一版只给 `repeated_failure` 加了失败身份核验，`no_progress` 仍按签名集合判
振荡。集成测试立刻暴露这是假缓解：签名被误聚合时，`repeated_failure` 被抑制了，
`no_progress` 却因为"签名回到过更早一轮"把同一个 run 停在完全相同的地方。

现在两条规则都要求签名**与**失败身份（node_id + 异常类型 + 类别）同时一致，
仅签名一致而身份不同只记 `signature_collision_suspected`，不作为停止依据。
这仍然不改 Phase 7 冻结的 SHA1 算法，**DA-010 保持 PENDING**（见第 6 节）。

---

## 4. Checkpoint 与中断恢复

### 4.1 实测结论（写下来是因为它反直觉）

* LangGraph 的 `durability` **默认是 `"async"`**。实测（同进程 + `os._exit`）在
  默认值下，节点已经执行完、checkpoint 却还没落盘，恢复只能从头重跑——G-13 的
  "不重复已完成节点"根本不成立。因此运行入口显式使用 `durability="sync"`。
* `durability` 必须是 `invoke` 的关键字参数，放进 `config` 里会被静默忽略。
* 节点是 `async def`，所以只能用 `ainvoke`；同步 `SqliteSaver` 在 `ainvoke` 下
  直接 `NotImplementedError`，因此改用 `AsyncSqliteSaver`（`aiosqlite` 是
  `langgraph-checkpoint-sqlite` 的既有依赖，未新增包）。
* `AsyncSqliteSaver.__init__` 会抓 running loop，所以 `open_checkpointer` 是协程。
* "下一步该跑哪个节点"必须读 `await graph.aget_state().next`，不能读原始
  `checkpoint["next"]`（实测崩溃后为 `()`）。

### 4.2 恢复三段

SQLite 里存的只是 **state**。Workspace 的补丁、容器跑出来的结果、Artifact 目录里的
JUnit 都在 checkpoint 之外，因此恢复流程是：

1. `checkpoint_state()` 按 `thread_id = run_id` 读回 State；
2. `verify_recovery()` 核对 Workspace 是否存在、base commit 是否一致、当前补丁哈希
   是否与 checkpoint 相符、已记录的执行证据是否仍可读；
3. `resume_run()` 只对**证据仍成立**的部分做复用；证据失效就关掉复用开关
   （`deps.evidence_trusted`），让该轮重新执行并重新计费。

为了让第 2 步在崩溃后真有东西可核对，`run_target_tests` 在**当轮**就把执行结果写成
`attempts/NN/execution.json`。只在 finalize 写的话，一次崩溃的 run 一个执行 Artifact
都没有，恢复侧只能判"证据缺失"，把已经做成的事重跑一遍。

### 4.3 承诺的边界（不是 exactly-once）

崩溃如果落在"外部副作用已完成、checkpoint 还没写"的窗口里，恢复后这一轮会重做。
幂等身份由 `run_id` + iteration + patch hash + 目标测试集合组成，用来判断"要不要
重做"，但它给不了 exactly-once——一个没有两阶段提交的系统这样宣称就是假的。
这里承诺的是 **at-most-once-with-verified-evidence**：证据有效就不重放，证据无效就
重来一次。

Workspace 缺失或 base commit 对不上时**拒绝继续**（`status=error` +
`workflow_error`），因为继续跑会产出一个和 checkpoint 不属于同一个世界的补丁。

---

## 5. 门禁与验收证据

| 检查 | 命令 | 结果 |
|---|---|---|
| Lint | `ruff check .` | exit 0 |
| Format | `ruff format --check .` | exit 0 |
| 类型 | `mypy src`（strict） | exit 0，144 files |
| 离线全量 | `pytest -m "not docker and not llm" --cov` | exit 0：**2419 passed / 0 failed / 1 skipped / 30 deselected**，覆盖率 **93.81%**（阈值 80.0%），1524.03s |
| Phase 8 定向 | `pytest tests/graph tests/unit/graph` | exit 0：**301 passed / 0 failed**，560.42s（含 1 项 docker 标记用例） |
| 真实 Docker 两轮 E2E | `pytest tests/graph/test_phase8_docker_e2e.py -m docker` | exit 0：**1 passed / 28.61s** |

环境：Docker Desktop，Engine 29.8.2 / `OSType=linux`。

**离线全量第一次运行是红的，且不是本轮引入的**：`tests/unit/repo/test_gitcmd.py::
test_timeout_kills_the_grandchild_not_just_the_child` 失败（Phase 1 的孙进程树清理
用例，超时预算 1.0s、轮询上限 6.0s、`elapsed < 3.5s`，三个数字在满负载下都会踩线）。
本轮没有改动 `repo/gitcmd.py`，也没有改动该测试文件。隔离重跑三次均通过：77.59s、
8.90s、137.99s——同一个用例的墙钟差 15 倍，说明它是负载敏感的计时断言，不是逻辑
回归。第二次全量运行 2419 passed / 0 failed。处理方式：**没有 skip、没有放宽断言、
没有降低阈值**；把它作为已知不稳定用例登记在这里，收紧计时预算属于 Phase 1 的
测试维护，不在本轮范围内。

FakeLLM + FakeSandbox 多轮通过率**不是**真实模型的自主修复成功率；真实模型链路
（P-24）本轮仍然 NOT RUN。

---

## 6. 遗留风险与 Deferred Acceptance

本轮**未关闭任何**历史待验收项。

| ID | 状态 | 本轮变化 |
|---|---|---|
| DA-001～DA-006 | MITIGATED / NOT RUN / PENDING，维持原状 | 不在本轮范围 |
| DA-007 | 已关闭（Phase 4） | 保持关闭 |
| DA-008 | NOT RUN | 复现节点用的是 FakeLLM 脚本候选，不构成模型复现能力证据 |
| DA-009 | OPEN | 多轮修复未扩大新增测试权限；复现目录仍被 diff 层剔除。"新增测试文件进入交付补丁"的政策仍在 Phase 9 定稿 |
| DA-010 | **PENDING** | 签名算法未改；停止判据加了失败身份核验（3.2 节），碰撞时只记录不终止。仍**没有**真实多轮数据上的误聚合/漏聚合统计 |
| P-24 | NOT RUN | 真实 LLM 修复链路未运行 |

其他已知边界：

* **Refine 无法撤销已落盘的错误写入**。模型若把受保护文件写进 Workspace，refine
  只会往上叠加，第 2 轮不可能凭空变干净；真实的恢复路径是连续无效直到
  `validation_loop`。`test_path_validation_retry.py` 按这个事实断言，而不是写成
  "无效后立刻变有效"。
* **Phase 9 未实现 ⇒ 没有 M2 结论**。`provisionally_verified` 只代表"一份通过校验的
  补丁在授权测试上通过"，不代表回归安全或 Issue 已解决。
* `run_regression` / `review` 的停止规则接缝已就位，但 Review 高危问题"只触发一次
  额外修复"的上限属于 G-32，本轮由迭代预算与 `recursion_limit` 兜底。
* `expand_scope` 受 `LocalizationResult` 的 3 文件 / 5 symbol 上限约束，超出部分
  被丢弃并把理由写进事件；这不是缺陷，是 Context 预算的一部分。

---

## 7. Phase 9 接入点

`Deps.regression_runner` / `Deps.reviewer` 是协议字段，注入实现后：

* `run_regression` 写入真实 `RegressionResult` → 路由自动按"有无新增失败"分诊；
* `review` 写入真实 `ReviewResult` → 高危问题回到 `check_budget`；
* `ProgressSnapshot.regression_measured` 由 `True` 默认值恢复参与进度比较的语义，
  无需再改 schema；
* `finalize` 的保守成功判定可以升级为"目标 PASS ∧ 无新增回归 ∧ 无未解决高危"。

StateGraph、Attempt 历史、预算与 Checkpoint 都不需要改。
