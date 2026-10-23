# Phase 15：M3 正式评测执行计划

日期：2026-10-22。状态：**PLAN ONLY / 未执行任何批量付费评测**。
本计划建立在 [`docs/experiment-plan.md`](../experiment-plan.md) 的数据集、费用公式与
`SWEBenchRunner` / 官方 Harness 实现之上；本文件只补齐 Phase 15 需要冻结的执行条件、
归档规则与分层门禁，不重新实现评测器。

> 边界：GLM11–GLM14 是同一实例 `marshmallow-code__marshmallow-1343` 的 4 次重复运行，
> 只能作为链路与费用量级参考，**不得用于推导 SWE-bench 解决率**。FakeLLM、MockTransport、
> 离线演示、配置完整或 Live 准入通过都不计入真实模型成绩。

---

## 1. 冻结事实

| 项 | 冻结值 | 来源 / 校验 |
|---|---|---|
| 数据集 | `princeton-nlp/SWE-bench_Lite` | `src/codepilot/eval/swebench.py` |
| revision | `6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2` | 代码钉死；manifest 记录 |
| 官方 split | dev 23 / test 300 | manifest |
| 官方 Harness | `swebench==4.1.0`，独立 `.venv-swebench` | `configs/swebench/env-specs-4.1.0.json` |
| 环境规格 | `configs/swebench/env-specs-4.1.0.json` | 与 harness 版本强绑定 |
| dev30 | `configs/subsets/swebench-lite-dev30.json`，30 题 | manifest hash 见下 |
| test50 | `configs/subsets/swebench-lite-test50.json`，50 题 | 冻结后不用于调参 |
| official dev23 | `configs/subsets/swebench-lite-official-dev23.json`，23 题 | 只作参考 |
| 模型 | BigModel `glm-5.3-flash`（别名 `glm-5.3-flash-main`） | `configs/llm.yaml`，2026-10-18 价目 |
| LLM 单价 | CNY 0.8 / 2.8 / 0.23 每百万 input/output/cached | 公开价目快照；跑批时写进 manifest |
| 测试环境 | Linux / WSL2 Ubuntu-24.04；Docker；Python 3.12 | 官方 Harness 原生 Windows 不可用 |

子集文件 hash（`configs/subsets/manifest.json`）：

- `swebench-lite-dev30.json`：`18a0ccc75edd93a2859160f3256e797daf22f9be0035033ad4abd83981538d48`
- `swebench-lite-test50.json`：以 manifest 中同名条目的 `sha256` 为准；启动前重新核对
- 实例清单本身也有独立 `instance_list_sha256`，必须与内容 hash 一起写入批次 manifest

---

## 2. 实例选择与分层门禁

### L0：零费用预检（进入所有层的条件）

- `uv run codepilot config check`：模型、单价、币种、密钥引用通过。
- 对目标实例执行 `codepilot bench-swebench ... --dry-run`：Loader、环境规格、M2 装配、
  Prediction、官方命令与产物路径均通过；不调用 LLM、不建镜像、不跑 Harness。
- Docker 可用（`docker info` 与官方 Harness 镜像链路），官方 Harness 版本为 4.1.0。
- `docs/06-security.md` 的凭据边界未变；费用授权、单题硬上限和批次总上限已书面确认。
- 记录当前 Git commit、配置 hash、模型价目来源；未满足任一项则 **BLOCKED**，不启动 L1。

### L1：1 个全新实例（端到端）

- 实例：`pvlib__pvlib-python-1072`（official dev，未出现在 GLM11–GLM14、dev30/test50）。
- 目标：验证真实 Provider → Docker → M2 → Patch → 官方 Harness 全链路，不用于解决率。
- 运行：`--split dev --repeats 1 --live-llm`，预算硬上限 **CNY 3.00**。
- 停止条件：
  - Key 出现在任何前端/日志/事件/artifact → 立即停止并清理现场；
  - 费用触顶、超时、官方 Harness 不可执行或出现未分类错误 → 记失败分类，不重试求成功；
  - 链路成功但官方 UNRESOLVED → 这是有效结果，不因结果不好而重跑。
- 证据：run manifest、task 快照/hash、patch、prediction、官方 report、usage/journal、
  stop reason、Docker 版本与镜像 ID、墙钟、实际费用。
- 通过后才允许进入 L2。

### L2：10 题费用/口径校准

- 实例：从 dev30 冻结 10 题，覆盖 10 个不同 repo，按 dev30 冻结顺序取每个 repo 的第一题：
  - `astropy__astropy-12907`
  - `django__django-11910`
  - `matplotlib__matplotlib-23314`
  - `mwaskom__seaborn-3010`
  - `pallets__flask-4045`
  - `psf__requests-2148`
  - `pydata__xarray-4493`
  - `pylint-dev__pylint-6506`
  - `pytest-dev__pytest-11148`
  - `scikit-learn__scikit-learn-12471`
- 启动前把该清单写成 `configs/subsets/swebench-lite-cal10.json` 并记录两个 hash；写入后不得
  因结果调参。该文件目前 **尚未创建**，是 L2 的进入条件，不是已完成项。
- 运行：每题 1 次，预算硬上限 **CNY 3.00/题**，批次总硬上限 **CNY 30.00**。
- 产出：`c̄`、标准差、最大值、未知费用任务数、请求数、官方/内部判定分布；这些只用于
  预算与稳定性，不作为 M3 效果结论。
- 停止条件：任一题未知费用 >0（整批总额变下界）→ 停止并修复计费；实际均值 > 规划值
  2 倍、基础设施失败 >1 题、或出现 Key 泄漏 → 停止；不得用重跑剔除坏结果。

### L3：正式 M3（冻结 test50）

- 实例：`swebench-lite-test50.json` 的 50 个官方 test 实例，单次运行，不挑选、不替换。
- 运行：每题 `--split test --repeats 1 --live-llm`，预算 **CNY 3.00/题**，
  批次总硬上限 **CNY 150.00**；并发 **1**；官方 Harness 单实例超时 1800 秒。
- 进入条件：L2 完成且费用/口径稳定；配置、价格表、子集 hash、Harness 版本、Docker 镜像
  基线和运行参数全部冻结；书面授权批次总费用。
- 停止条件：任一安全边界事件、连续 3 题基础设施失败、实际累计费用达到批次上限、
  或官方 Harness 报告不可解析 → 停止；已完成的唯一一次运行保留，不重跑刷分。
- 结果表：官方 RESOLVED/UNRESOLVED/empty/apply-fail 与内部 M2 终态/stop reason 分列；
  解决率分母是 50 个唯一实例，不是运行次数。

### L4：M4 消融与扩展集

- 仅在 L3 完成且读数和费用稳定后启动。
- 最低 3 组（A/D/G），每组 test50 单次；预算按 `3 × 50 × 3.00 = CNY 450.00` 硬上限，
  实际预估按 L2 的 `c̄` 重新计算。
- test300 是独立授权层：必须在 test50 结果稳定、失败分类可复现后，另写批次计划。
- M4 不得修改 test50/test300 清单；不得用同一实例的多次运行替代组间对照。

---

## 3. 单实例运行参数（冻结）

```text
entry:        uv run codepilot bench-swebench
provider:     bigmodel
model:        glm-5.3-flash-main
split:        dev (L1) / test (L2 用 dev30 文件、L3 用 test50)
repeats:      1
seed:         0
temperature:  0.0
max_output:   4096 tokens/request
timeout:      1800 s（Graph 与官方 Harness 各自）
budget:       3.00 CNY/instance（硬上限）
docker:       cpus=2.0, memory=4g
concurrency:  1（同一批次串行；不并行占多个 Docker 资源）
resume:       仅基础设施/checkpoint 恢复；生成新 run_id 并保留旧现场
record_llm:   L1 可选；L2/L3 默认关闭（避免把仓库内容写入额外副本）
```

调用次数没有单独 CLI 参数；硬边界由 `max_total_tokens=1,000,000`、`max_cost=3.00`、
`max_runtime_seconds=1800`、`max_iterations=5`、每轮工具调用 30、Reviewer 2 次共同约束。
批次监控另设**异常阈值 80 次 LLM 调用/实例**（历史单题最高 45 次）；超过阈值不自动重跑，
而是停止该实例并人工检查预算/循环异常。

---

## 4. 归档规则（每次运行一份不可变目录）

`artifacts/swebench/<batch_id>/<instance_id>/<run_id>/` 至少包含：

| 文件 / 字段 | 内容 |
|---|---|
| `manifest.json` | dataset/revision、subset hash、instance list hash、模型与价目、配置 hash、harness 版本、Docker 镜像 ID、Git commit、参数、时间 |
| `task/` | Issue、base commit、环境规格、M2 快照引用 |
| `patch.diff` | 最终 patch；空 patch 单独标记 |
| `prediction.jsonl` | 官方预测格式；每条唯一 instance_id |
| `usage.json` / `cost.json` / `dispatch-ledger.json` | token、调用数、费用、未知费用、派发对账 |
| `events.jsonl` / `journal/` | 时间线、stop reason、Checkpoint 与恢复记录 |
| `swebench_harness/` | 官方 stdout/stderr、run report、单实例 report、镜像/容器清理状态 |
| `report.json` / `report.md` | 内部 M2 终态与验证级别（与官方 verdict 分开字段） |
| `checksums.txt` | 上述文件 SHA-256；写入后只读 |

禁止覆盖已有批次目录。重跑使用新 `run_id` 并在 manifest 写 `rerun_of`；历史目录不修改。

---

## 5. 双口径与去重

### 5.1 官方 / 内部独立

- `official_verdict`：只来自官方 Harness 的 `resolved/unresolved/empty_patch/apply_fail`；
  apply 失败按官方报告规则归入 error，不私自改判。
- `internal_verification_level`：只来自 M2 closure（例如 `m2_verified`）。
- `stop_reason`：图/预算停止原因，独立保存。
- 三者不互相推导；报告必须同时展示，不能只留一个“通过/失败”。

### 5.2 唯一实例与唯一运行

- 解决率分母：test50 的 50 个唯一 `instance_id`，不是运行次数。
- 批次台账主键：`(batch_id, instance_id, config_hash, run_label)`；同一主键重复写入直接拒绝。
- A/B 或重跑只允许出现在 dev/L1/L2；test50 每组只取**预先指定的一次**运行进入主表。
- 任何额外运行标记 `excluded_from_solution_rate=true` 并保留原因；不得事后挑选最好的一次。
- GLM11–GLM14 的同实例重复运行不进入任何 M3 分母。

---

## 6. 失败分类与重试

| 类别 | 例子 | 处理 |
|---|---|---|
| `INFRA_DATASET` | 数据快照缺失/hash 不符 | 修复后从检查点重跑；记基础设施失败 |
| `INFRA_DOCKER` | 镜像构建失败、Docker 不可用 | 修复环境后重跑；不计模型失败 |
| `HARNESS` | 官方命令失败、报告不可解析 | 保留现场，分类为评测基础设施 |
| `LLM_AUTH` | 401/403、凭据不可用 | 停止批次；不重试求成功 |
| `LLM_RATE_LIMIT` | 429/5xx | 现行重试策略内处理；超限记模型/供应商失败 |
| `BUDGET` | token/费用/测试次数/时长触顶 | 有效内部结果，按 stop reason 保留 |
| `MODEL_OUTPUT` | 结构化输出失败、空 patch、patch apply 失败 | 有效模型结果，不重跑刷分 |
| `SECURITY` | Key 出现在日志/artifact | 立即停止并清理；按事故处理 |

重试必须有上限和书面原因；test50 不因“结果不好”重跑。恢复只从 Checkpoint 续跑，
不重置已发生费用；`SWEBenchRunner` 的 task/graph/evaluation 三层恢复沿用 Phase 11。

---

## 7. 费用估算

沿用 [`docs/experiment-plan.md`](../experiment-plan.md) 的公式：

```text
task_cost = (input - cached_input) × input_per_mtok / 1e6
          + cached_input × cached_input_per_mtok / 1e6
          + output × output_per_mtok / 1e6
estimated_total = c̄ × task_count × groups × seeds + rerun_reserve
```

- `c̄` 必须来自 L2 的 10 题**实际**费用，同时报告 `mean ± t·SE`、`max(c_i)`、
  `cost_tasks_unknown` 与 `total_cost_is_lower_bound`。
- 在 L2 完成前，只能用 GLM11–GLM14 的单实例真实费用作**量级上界参考**
  （最高 0.28167776 CNY/次）；它不是 SWE-bench 均值。按 50 题、1 次、该上界估算的
  LLM 费用约 **CNY 14.09**；加 15% 重跑预留约 **CNY 16.20**。这是估算上界，不是承诺。
- 硬上限按每题 3.00 CNY 执行（50 题 = 150.00 CNY）；估算值与硬上限同时写入批次 manifest。
- 官方 Harness 的 Docker 构建/执行时间、下载量、磁盘与云资源费用是**另一本账**，
  不与 LLM token 费用合并。
- 未知费用任务 >0 时，整批总额标为下界；不得把未知当 0。

---

## 8. 当前状态与下一步

| 项 | 状态 |
|---|---|
| L0 无费用预检 | PARTIAL：本地配置/凭据预检完成；Docker 命名管道当前拒绝访问，Harness dry-run 未执行 |
| L1 单实例真实 E2E | NOT RUN（等待费用授权与 Docker 修复） |
| L2 10 题校准 | NOT RUN；cal10 文件尚未冻结 |
| L3 test50 | NOT RUN |
| L4 M4 / test300 | NOT RUN |
| 本阶段实际费用 | **CNY 0.00**（0 次付费 Provider 调用） |

下一步顺序：修复 Docker 命名管道访问 → 在 Linux/WSL2 执行 `--dry-run` → 书面确认 L1
费用上限（CNY 3.00）→ 仅执行 `pvlib__pvlib-python-1072` 一次 → 通过后再冻结 cal10。

## Phase 18 更新（2026-10-22，原 Cal10 历史快照）

- 内部 M2 已恢复并用 Phase 16 原补丁/预测完成一次零模型真实 Docker 验证（收集 1 项、未修复复现 FAIL、打补丁复现/目标/回归 PASS、清理通过）。该恢复是生产组件级：使用 `OfficialInstanceSandbox`、生产 JUnit/status 解析和 `execution_route`，没有运行 `InferenceAssembly`/完整 LangGraph。
- 生产执行镜像改为按实例解析并校验完整 ID；只有 `pvlib__pvlib-python-1072` 使用 Phase 17 修复镜像，其他实例不得套用。
- L2-Cal10 已真实执行 7/10：有效官方评测 5，RESOLVED 2，有效官方解决率 40.0%；内部链路形成终态 3/10（seaborn、flask、requests），其中 seaborn 为 `failed/tests_failed`、requests 为 `stopped/diagnosis_give_up`，不等于验证通过；内部声称成功 1/10（flask）。
- 停止于 `pydata__xarray-4493` 第 3 次调用 `ReadTimeout`：派发账本 `unknown_frozen`，保留 CNY 0.811469 上界并停止后续派发；`pylint`、`pytest`、`scikit-learn` 未执行。
- 已结算 CNY 1.056919，记账上界 CNY 1.868388，剩余 CNY 28.131612；单题与整批硬上限均未突破。xarray 超时调用的实际用量不可见；费用对账按保守上界完成（`reconciliation_complete=true`、`unreconciled_cost_calls=0`、`actual_usage_unknown_calls=1`），不能声称与服务商账单一致，也不按零计费。
- 后续进入条件：先解决 xarray 的未结算费用（不得重置/拆分账本、不得按零计费），再决定是否补跑后 3 题；不得复用本轮现场或历史预测。

## Phase 18E 更新（2026-10-23/24，l2cal10e 最终口径）

- L2 的 pytest 子项最终状态：**`NOT VERIFIED`**。最后 attempt `l2cal10e-a21` 的内部验收为 `REJECTED`；真实 Docker 图运行已发生，但目标测试未通过、完整回归未完成、Reviewer 未执行。停止原因是模型没有得到满足六项验收的补丁（`repeated_patch`），不是预算、环境或 Harness 故障。
- **官方 Harness 未运行**：pytest 的官方字段固定为 `NOT_EVALUATED` / `DEFERRED`，不得写成 `UNRESOLVED`、`RESOLVED` 或「已评测」。sklearn 的既有结果为内部 `ACCEPTED`、官方 `UNRESOLVED`；本轮未重跑 sklearn、xarray、pylint。
- L2 费用口径：pytest 单题累计上界 `CNY 7.80709616`；sklearn `CNY 0.62403504`；批次已结算 `CNY 7.18344160`；未知预留 `CNY 4.05734400`（5 次，绝不按零计费）；批次累计上界 `CNY 11.24078560`；pytest 剩余单题预算 `CNY 12.19290384`；批次剩余预算 `CNY 88.75921440`。20/100 护栏未触发。
- 证据口径：以 a21 manifest / ledger / acceptance / report / batch status 与新版 `attempts.json` 为准。顶层 `cost-ledger-reconciliation-l2cal10e.json`、`official-evaluation-summary-l2cal10e.json`、`phase18e-summary.json` 只覆盖 a9 时点，不能充当 a10–a21 的最终汇总。
- 进入 L3/L4 的条件没有改变，且更严格：在 pytest P2 未验证、官方 Harness 未运行的前提下，**不得继续派发 a22 或任何新实时运行**；任何补跑或升级都需要新的书面授权与新的预算口径确认。
