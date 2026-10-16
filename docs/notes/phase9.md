# Phase 9 — Regression, Review and Run Report

日期：2026-10-15～2026-10-16。基线：`4fb83fb`（Phase 8 验收提交）；分支：`codex/phase9-regression-review`。

## 验收边界

本轮仅本地开发；无 Push、PR、Merge，无真实付费 LLM / Embedding 调用。
G-37、P-24 保持 **NOT RUN**。FakeLLM 审查只证明工程调用及路由，不证明真实模型审查质量。
G-25～G-36 **完成**，M2 **离线工程验收通过**。最终全量门禁 exit 0；真实模型效果不在本次通过范围内。

## 实现与证据索引

| 项目 | 实现 | 证据 |
|---|---|---|
| G-25 | 扩展既有 `schemas/regression.py`，保留旧字段，默认 complete=false / NOT RUN；baseline 身份、执行/未执行范围、原有失败、状态变化、重试、seed | `test_phase9_regression.py` round-trip / 参数化基线判定 |
| G-26 | `graph/regression.py`，复用 L1 节点、AccountingSandbox、BudgetManager；L2 影响测试、L3 授权基线测试集合，每任务最多两次全量 | FakeLLM 完整图、四个真实 Docker 案例 |
| G-27 | passed→failed/error 才为新增回归；精确 NodeID，排除 baseline flaky；缺失或不一致基线停止验证 | 16 组 outcome 笛卡尔积、不同参数 NodeID 负控制 |
| G-28 | 每疑似新增失败最多两次重试；任一 passed 标记 flaky；环境/范围错误中断 | 四组重试序列、预算不足、真实受控 Flaky |
| G-29 | 按 baseline 总运行时长估计；固定 seed，从 L2 以外候选抽取 ceil(20%)；明确 sampled | 抽样披露测试 |
| G-30 | 复用 `schemas/review.py`；severity 枚举、路径/行号检查、结构化输出 | 非法证据拒绝、JSON 结果 |
| G-31 | `agents/reviewer.py`；ContextBuilder 24k、LLMProvider/FakeLLM、无写工具/无 shell、真实 diff 与源码证据校验 | 完整图、无回归证据零调用、伪造文件拒绝 |
| G-32 | high 才允许额外修复，RepairState.review_repairs 硬上限 1；再次 high 停止 | high→批准、high→high；low/medium 拒绝不修复 |
| G-33 | `fixtures/repos/regression_break` | Docker 中目标通过而 other 失败；Diagnosis give_up 合法停止 |
| G-34 | `fixtures/repos/baseline_failures` | Docker base failed/patch failed 单独披露，不计新增 |
| G-35 | `fixtures/repos/flaky_suite` | Docker 内 pytest 真执行；可信测试驱动改变环境输入产生确定性 FAIL/PASS，不依赖时序 |
| G-36 | 扩展唯一 RunReport，汇总 baseline/reproduction/attempts/regression/review、用量、耗时、验证等级与边界 | 图测试 JSON/Markdown 产出与状态一致 |

## 路由及预算

L1 FAIL → diagnose；环境错误 → finalize。L1 PASS → L2 → L3/seeded sample → flaky 重试 → review。
确认新增回归 → diagnose；不完整执行 → finalize failed；不会因为零失败列表而把未执行阶段记为成功。
Review 低/中意见保留但不触发额外修复。有效 high → check_budget → repair_patch → validator → tests，最多一次；仍有 high → stopped。

沿用既有全局状态枚举：SUCCEEDED 的报告使用 `m2_verified` 或 `sampled_verified` 区分 full/sample；未配置回归/Review 的旧路径保持 provisionally_verified。已有失败会在报告保留；M2 无新增回归不等价于全部原有测试通过。
L1、基线两次、L2、L3、重试均走同一 AccountingSandbox。基线先预留两次 test_runs，且两次执行之间重新检查预算；每次回归/重试执行前检查预算并将容器 timeout 限制在剩余墙钟内。

## 固定保存的真实 Docker 证据

最终保留证据运行：`artifacts/phase9-docker-verified.log`，**4 passed / 184.96s，exit 0**。
摘要：`artifacts/phase9-docker-evidence-summary.json`；完整 JSON/Markdown 报告、JUnit、
容器执行输出和操作日志位于 `artifacts/phase9-docker-verified/`，全部 Git 忽略。

| 案例 | 实际 test_runs（含两次 baseline） | 既有测试 NodeID 数 | 判定 |
|---|---:|---:|---|
| normal | 5 | 2 | L1/L2/L3 完成、FakeLLM Review，m2_verified；授权新增测试交付但不作成功依据 |
| regression_break | 6 | 2 | other 新增回归、两次重试均失败、Diagnosis give_up → stopped，Review NOT RUN |
| baseline_failures | 5 | 3 | 原有 test_existing_failure 保留并披露，new_failures=[]；不宣称所有测试 PASS |
| flaky_suite | 7 | 3 | 初次失败、两次受控重试，标记 test_controlled_flaky；L3 完成 |

每个案例均验证原始 fixture Git 工作树干净。真实 Docker 与 FakeLLM 事实分别记录，
本轮没有真实模型效果验收。

## 执行日志与恢复

`graph/operations.py` 在现有 ArtifactStore 的 run 目录保存轻量 SQLite 日志；operation_id 为轮次+阶段/测试重试身份。
执行前提交 intent；执行后提交脱敏结果、累计用量、结果摘要及外部 Artifact 哈希。
身份绑定 run/task/base/workspace/patch/baseline/environment。恢复核验身份和哈希后复用完成结果，以累计账本最大值对齐测试次数，而非重复累加。
LangGraph SQLite Checkpointer 继续使用 Phase 8 实现，未重建图或状态机。

独立进程测试覆盖回归已提交、Review 已持久化但 Checkpoint 未提交、执行中、Artifact 已写盘及证据损坏。
不确定 intent 拒绝自动重放；这会要求操作员核实，不能宣称 exactly-once。Review 异常报告 usage_uncertain，零费用读数不代表已确认免费。

## 历史风险

- **DA-009：FIXED / CLOSED（2026-10-16）**。可信 `WorkspacePathPolicy.allow_new_tests` 默认 false；模型参数无授权能力。PatchValidator 独立拒绝未授权新增测试，AST 检查新增空/恒真测试和 skip/xfail。最终交付还要求 Review；复现目录始终排除。目标与回归成功证据来自基线授权测试或已验证复现，不接受 Agent 新增测试自证。
- **Checkpoint：MITIGATED**。持久化完成结果可验证复用；未确认的外部执行/模型费用仍有无法消除的不确定性。
- **DA-010：PENDING / MITIGATED**。FailureSignature 算法不变，联合身份补充原始消息；同 NodeID/异常类型但不同消息不会触发 repeated_failure。受控正负样本不构成真实模型多轮故障诊断质量证据，不关闭。
- **Phase 1 进程树测试：FIXED（定向测试）**。替换时序标记推断为启动握手、父子 PID 与带上限退出查询；无负载/两个 CPU 负载进程均验证清理，故意只杀父进程的负控制验证探针能发现残留。21 passed；未改 Git 清理生产逻辑。独立提交 `795367d`，后续 `bbace85` 恢复原始 LF，未 amend。
- DA-001～DA-006 保留；DA-007 已关闭；DA-008 未执行真实模型；P-24/G-37 NOT RUN。

## 已执行测试（中间记录）

- 首批定向：18 passed。
- 扩展兼容：369 passed / 2 failed；两个失败是旧测试前置条件依赖已取消的默认行为。仅调整 complete=true 和可信新增测试授权，保留原断言；随后定向 105 passed。
- 回归/Review/独立进程恢复：36 passed / 182.89s。
- 真实 Docker 四案例：4 passed / 166.45s，FakeLLM，与离线测试分开。
- 进程树：21 passed / 38.57s。
- 首次测试遇到临时目录权限拒绝：17 passed / 1 setup error；获准隔离临时目录执行后通过，不是产品测试失败。
- 2026-10-16 定向恢复/回归/权限及既有停止路径：52 passed / 348.26s。
- 2026-10-16 Docker 四案例（含授权新增测试交付）：4 passed / 265.38s；预算补强后的最终 Docker 复测：4 passed / 180.17s，exit 0。
- 首次全量离线运行出现 3 个旧 positive fixture 失败：其不同数字消息现在被正确视为证据冲突。将重复失败正例的原始消息保持一致，原断言不变；不同消息另有负控制。该全量运行因后续生产代码修改而取消（exit 1），没有完整结果，不算通过。原日志保留 `artifacts/phase9-offline.log`。
- 最终静态门禁：Ruff exit 0、Format exit 0、mypy exit 0（147 source files）。
- 进程树清理追加三轮重复：每轮 3 passed / 18 deselected，耗时 27.94 / 13.09 / 15.00s；包含无负载、两个负载进程、只杀父进程的负控制。
- 报告负控制追加 8 passed：仅 succeeded 标签而缺目标、Validator、完整回归、Review 或仍有 high/new_failure，均不能得到 M2 验证等级。
- 2026-10-16 首次完整门禁在图及 Phase 9 恢复测试通过后，因发现 Report 仅依赖 succeeded 标签的边界而取消（日志 `phase9-offline-final.log`，exit 1）。补强报告独立证据核验后重跑最终全量 `phase9-offline-verified.log`；不是把取消运行计为通过。
- 完整离线首个完成运行 `phase9-offline-verified.log`：**1 failed / 2480 passed / 1 skipped / 34 deselected，93.18%，exit 1，2761.97s**。唯一失败为 `test_termination::test_stop_reasons_are_all_reachable_and_distinct`：其重复失败与振荡场景仍使用不同原始消息。与先前正例修正同理，仅把正例消息改成一致；全部 StopReason 断言不变，DA-010 的冲突负控制保留。
- 最终全量离线 `phase9-offline-acceptance.log`：**2481 passed / 0 failed / 1 skipped / 34 deselected，1929.78s，exit 0，覆盖率 93.18%**。
- 唯一 skip 为既有本地 embedding 模型验收：未设置 `CODEPILOT_TEST_EMBEDDING_MODEL`，没有新增 skip，没有付费 Embedding 调用。
- 最终 Ruff / Format（310 files）/ mypy（147 source files）均 exit 0；覆盖率阈值 80% 未下调。生产源码哈希与最终 Docker、全量门禁一致。

## 接入与验证范围

构造既有 `Deps` 时注入 `regression_runner=RegressionRunner()` 和
`patch_reviewer=PatchReviewer(provider)`（兼容旧名称 `reviewer`）。生产执行使用已有
DockerSandbox，测试可注入 FakeSandbox；本轮模型统一 FakeLLM。旧 Phase 8 调用方未
注入组件时保留 provisional / NOT RUN，不会静默升级为 M2。没有新建第二套 StateGraph。

L1 复用原目标测试节点；L2 从 ImpactAnalysis 和实际 Git Diff 的同目录测试选择；
L3 全量范围为 base collection 建立的授权既有 NodeID 集合。新增测试、临时复现测试、
隐藏评测不进入该集合。新增测试可交付不等于它成为可信成功依据。
`regression_mode` 报告字段来自兼容 Schema 的 `mode`；`complete=false` 表示尚未完成
所选范围，不能只读 `new_failures=[]` 或默认 `mode=full` 就推导成功。

回归/审查结果进入 Attempt、RepairState、事件、JSON/Markdown Report。
所有真实测试与 Review 请求/结构化输出均通过已有脱敏写盘机制；运行 Artifact 在
忽略目录和隔离 pytest Workspace 中，不纳入 Git。报告包含补丁 SHA256、已执行和未执行
NodeID、原有失败、状态变化、重试证据、seed、用量未知标记与 FakeLLM 边界。

DA-010 统计包括 2 对纯离线联合身份样本，加上 2 对来源于真实 Docker 回归失败的受控
对照（保留原失败身份，数字消息扰动仍产生同一冻结签名）。两组各为：重复正例 1、
碰撞负例 1、误聚合 0、漏判重复 0。**总计 4 对受控样本，不是 4 个真实模型多轮任务**；
没有足够数据估计真实误聚合率或漏聚合率，DA-010 保持 PENDING。

## 本地 Git 与最终门禁绑定

- 基线：`4fb83fb`，已包含 Phase 8 验收提交。
- 分支：`codex/phase9-regression-review`。
- `795367d`：独立修复进程树清理测试。
- `bbace85`：恢复上述测试文件原始 LF，无 amend。
- `cce96b7`：G-25～G-36、DA-009、持久化操作证据与 Report 真实性实现。
- `e87df75`：旧停止条件测试的重复/振荡正例使用相同原始消息，所有断言保留；定向 3 passed / 51.08s。
- 最终门禁源码身份：`artifacts/phase9-tested-source-sha256.json`（147 个源码文件）。
- 最终离线收集：2516 项，排除 Docker/LLM 标记 34 项，执行 2482 项；相对 Phase 8 新增 66 个测试用例（其中 4 个 Docker）。
- 覆盖率阈值仍为 80%，未更改 pytest 配置、未新增 skip、未修改 FailureSignature 算法。
- 既有测试仅调整其过时的前置条件：显式声明已完成回归、可信新增测试授权、重复失败正例使用相同原始消息；原断言保留，冲突消息独立负控制。
- 没有 Push / PR / Merge，没有改写 Phase 0～8 提交。

新增测试策略关闭针对权限与交付边界；AST 检测属于启发式，不能证明任意代码没有作弊。
允许新增测试须由可信任务配置决定，并经过 Validator 与 Reviewer；新增测试本身不构成成功证明。

## 后续

本轮 G-25～G-36 与四项附加风险处理已完成到上述证据边界。可以进入 Phase 10 Synthetic Benchmark 的离线开发；本轮未开展 Phase 10，等待单独指令。

本轮未发现未解决的 P0/P1 工程阻断项；历史真实模型、Linux CI 等延期验收缺口仍按原优先级保留，不能据此宣称所有历史 P1 均已关闭。
