# 实验计划与费用模型（E-20）

> 状态：**ENGINEERING_IMPLEMENTED / REAL_COST_CALIBRATION_PENDING**
>
> 本文定义 SWE-bench Lite 实验的规模阶梯、费用公式、预算执行与记录口径。
> **本轮（Phase 11）没有调用真实 LLM、没有构建/拉取官方镜像、没有跑官方 Harness**，
> 因此文中所有 SWE-bench 成本/成功率数字都是**待实测（PENDING）**；
> 唯一有真实数字的是 Phase 10 Synthetic 39 题的 GLM-5.3-Flash 用量，它只作
> **工程参考**，不得当成 SWE-bench 均值（见 §8）。
>
> 相关实现：`src/codepilot/eval/swebench_runner.py`（费用/预算/恢复与报告指标）、
> `docs/notes/phase11.md`（本轮开发记录）、`docs/notes/phase10.md`（历史数据来源）。

---

## 0. 一页结论

* **规模阶梯**：1 题 smoke → 10 题成本校准 → 30 题开发集 → 50 题冻结测试集 →
  300 题完整 Lite。冻结集（test50 / 300）只用于报告，不做调参选择。
* **费用公式**（E-20 原文）：
  `estimated_total_cost = observed_mean_cost_per_task × task_count × experiment_groups × seeds`
  其中 `observed_mean_cost_per_task` 来自 **10 题真实校准**（本轮 NOT RUN），
  并且必须同时报告样本量、标准差、95% 区间与最坏单题成本。
* **两种成本分开记**：LLM token 成本（CNY/USD，按公开单价记账）与官方 Harness 的
  Docker 构建/执行成本（时间、下载量、磁盘、云资源费率）是两本账，**不合并成一个数**。
* **未知费用不是 0**：费用未知的任务让整批总额变成**下界**并单独计数；未知比例过高时
  停止无人值守批次。
* **10 题均值不是保证**：n=10 的均值只能作为预算起点；报告中必须给出
  `mean ± t·SE`、`max(c_i)` 与"未知费用任务数"。
* **历史 Synthetic 39 题**：真实 GLM 用量，但任务规模、环境、评测方式都与 SWE-bench
  不同，只能用于验证费用**记账方法**，不能代入 SWE-bench 预估，也不能声称解决率可比。
* **验收状态**：E-20 = `ENGINEERING_IMPLEMENTED / REAL_COST_CALIBRATION_PENDING`；
  10 题真实校准 **NOT RUN**；E-17 单题真实 smoke 保持 `LIVE NOT RUN`；M3 未达成。

---

## 1. 固定事实与数据来源

| 项 | 值 | 说明 |
|---|---|---|
| 数据集 | `princeton-nlp/SWE-bench_Lite` | 契约 §0 |
| Revision | `6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2` | 固定，不重新下载换版本 |
| 官方 split | dev = 23 行，test = 300 行 | 完整 Lite = official test 300 |
| 官方 Harness | `swebench==4.1.0`（`.venv-swebench`，不进主 venv） | 原生 Windows 不可 import（`resource`） |
| 环境规格快照 | `configs/swebench/env-specs-4.1.0.json` | 来自 `MAP_REPO_VERSION_TO_SPECS` |
| 开发集 | `configs/subsets/swebench-lite-dev30.json`（30 题） | 由 official test 300 分层抽样，seed `20261019` |
| 冻结测试集 | `configs/subsets/swebench-lite-test50.json`（50 题） | 与 dev30 不重叠，冻结后不用于调参 |
| 官方 dev 参考 | 23 题（`swebench-lite-official-dev23.json`，若已生成） | 只作参考，不与自定义划分混用 |

抽样规则（Owner E 实现，见契约 §9）：`instance_id` 升序、按 repo 分层轮转、两组不重叠、
不被单仓库主导，**不得按 GLM 成功率挑选**。子集文件的 hash 写进 manifest；换 seed 必须
生成新文件，不能在旧文件上"原地重抽"。

---

## 2. 规模阶梯与运行矩阵

题数不是"随便跑几个"：每一档回答一个不同的问题，混在一起会把成本校准变成调参。

| 阶段 | 题数 | 子集 | 重复 | 回答的问题 | 是否计入最终结论 |
|---|---|---|---|---|---|
| S0 单题 smoke | 1 | 任一真实 instance（推荐 dev 侧） | **A/B 两次独立 run** | 链路/命令/产物/预算是否真的通了；两次结果是否可复现 | 否（n=1 不进入任何均值） |
| S1 成本校准 | 10 | 从 **dev30** 内分层固定抽 10 | 1 | 每题平均费用 c、方差、请求数、未知费用比例 | 只用于**预算**，不作为效果结论 |
| S2 开发集 | 30 | `swebench-lite-dev30.json` | 1 | 开发期回归与失败案例分析 | 开发参考，不写进最终表 |
| S3 冻结测试集 | 50 | `swebench-lite-test50.json` | 每组 1 次（E-22） | 消融各组的主对比 | **是**（主结果） |
| S4 完整 Lite | 300 | official test 300 | 1 | 与公开榜单可比的最终读数 | **是**（最终读数） |
| 消融 | 7 组配置 A–G（最低 A/D/G 3 组） | 每组 50（S3） | 每组 1 seed | 每个组件是否真的有用 | 是（Phase 12 主表） |
| Synthetic 多 seed | 39 fixture | Phase 10 suite | 3 seeds | 图/指标/工具链回归 | 仅工程回归 |

**成本校准与冻结集隔离**：10 题校准必须从 dev30 抽，不能从 test50 抽。否则"看过成本/
结果再决定跑法"会污染冻结集，让 50 题不再是独立测试。

**重复次数**：SWE-bench 子集每组 1 次（E-22 规定）；多 seed 只用于 Synthetic。若要做
方差分析，只能在 dev30 上加 seed，并明确"这是开发集方差，不是冻结集结论"。

---

## 3. LLM 费用模型

### 3.1 记录字段（每个任务一行）

费用不是从"感觉"来的，必须能从记录逐项复算。`SWEBenchRunner` 写入的 journal 记录与
`RunReport.metrics()` 提供：

| 字段 | 来源 | 说明 |
|---|---|---|
| `input_tokens` / `output_tokens` / `total_tokens` | `Usage` | 失败任务也记录已发生的用量 |
| `cached_input_tokens` | `Usage` | 缓存命中输入；**已含在 input 内**，不重复相加 |
| `llm_calls`（请求数） | `Usage` | 含 provider 重试（02 §5.4：重试不计迭代，但计费用） |
| `cost_unknown_calls` | `Usage` | 服务商未回传用量的调用数；>0 时总费用是下界 |
| `cost`（任务费用） | `Usage.cost` | 公开单价 × 用量；`None` = 未知，**不是 0** |
| `cost_currency` | 配置 | 默认 CNY；混币种会被指标层拒绝 |
| `runtime_seconds` / `iterations` / `stop_reason` | 记录 | 用于每轮诊断、超时与预算分析 |
| 官方评测耗时/成本 | `artifact_refs["official_runtime_seconds"]` 等 | 与 LLM 账本**分开**统计 |

`RunReport.metrics()` 的 `cost` 段已经给出：总额、每任务均值、每解决任务均值、每请求
均值、每迭代轮均值、`cost_tasks_unknown`、`total_cost_is_lower_bound`、`cost_currency`；
`tokens` 段给出输入/输出/总量与每任务均值。**实验报告必须从这些键取数**，不允许从
Markdown 或日志里手抄。

### 3.2 单价与币种（2026-10-18 记录，来自 `configs/llm.yaml`）

单位：元（或美元）/ **百万 token**。价格会变，每次跑批必须把当时的价目与来源写进
manifest / 实验报告；不能沿用旧价目而不声明。

| 模型别名 | 官方模型 | 币种 | fresh input | output | cached input | 来源 |
|---|---|---:|---:|---:|---:|---|
| `glm-flash`（推荐跑批） | BigModel `glm-5.3-flash` | CNY | 0.8 | 2.8 | 0.23 | https://docs.bigmodel.cn/cn/guide/start/pricing |
| `glm-main` | BigModel `glm-4.7` | CNY | 4.0 | 16.0 | 0.8 | 同上 |
| `glm-5` 家族 | BigModel `glm-5` / `5.1` / `5.3` | CNY | 6.0 / 8.0 | 22.0 / 28.0 | 1.5 / 2.0 | 同上 |
| `zai-glm-4.7` | Z.AI（国际站）`glm-4.7` | USD | 0.6 | 2.2 | 0.11 | https://docs.z.ai/guides/overview/pricing |
| `zai-glm-5.3` | Z.AI `glm-5.3` | USD | 1.4 | 4.4 | 0.26 | 同上 |
| `deepseek-chat` | `deepseek-flash` | CNY | 2.0 | 8.0 | 0.04 | https://api-docs.deepseek.com/zh-cn/quick_start/pricing （高峰价） |

计算方式（与 Phase 10 一致）：

```
task_cost = (input_tokens - cached_input_tokens) × input_per_mtok / 1e6
          +  cached_input_tokens                 × cached_input_per_mtok / 1e6
          +  output_tokens                       × output_per_mtok / 1e6
```

大模型推理默认 `reasoning_effort` 与 `max_output_tokens` 必须写进 manifest；同一模型不同
推理强度是不同的实验条件，不能混进一个均值。

### 3.3 费用公式

设一次批次的 10 题校准里，第 i 个任务的实际费用为 `c_i`（只统计**费用已知**的任务，
n 为这些任务的个数）：

```
c̄               = (Σ c_i) / n                       # observed_mean_cost_per_task
estimated_total_cost = c̄ × task_count × experiment_groups × seeds     # E-20 公式
```

派生口径（报告里必须分别给，不能只给一个"平均")：

```
mean_cost_per_task        = c̄
mean_cost_per_resolved    = (Σ_{resolved} c_i) / N_resolved        # 条件口径
mean_cost_per_pair        = (Σ c_i) / N_pairs                       # 条件口径
mean_cost_per_request     = (Σ c_i) / (Σ llm_calls_i)
mean_cost_per_iteration   = (Σ c_i) / (Σ iterations_i)             # 每轮修复/诊断
mean_cost_per_attempt     = (Σ c_i) / N_tasks_with_llm_calls
```

* `mean_cost_per_resolved` 的分母是**官方 RESOLVED 的任务数**，不是派发数；它回答
  "每成功一次花多少"，不能拿来替代每任务成本。
* `mean_cost_per_pair`（若做 A/B 配对）只在成对任务上算，避免用不同分母相除。
* 多 seed：先按 seed 各自算 `c̄_seed`，再报 `mean ± 样本标准差`（ddof=1）；只有 1 个
  seed 时标准差无定义，报 `None`，**不要报 0**（那会被读成"完全一致"）。
* 消融：
  `estimated_ablation_cost = c̄ × 50 × experiment_groups × seeds`，
  最低 A/D/G 三组时 `experiment_groups=3`；同时把每组预算上限按 `c̄_upper` 计算。
* 整批（含重复/消融）总预算 = Σ 各阶段公式值 + 校准批次自身 + 重跑预留。
  重跑预留建议单列（例如固定 10%），**不要**把它偷偷揉进 `c̄`。

### 3.4 10 题成本校准流程（本轮 NOT RUN）

1. 从 dev30 按 repo 分层固定抽 10 题（seed `20261019`），记录子集 hash；不按模型结果挑。
2. 用与正式批次**相同**的模型、单价、`max_output_tokens`、推理强度、预算参数跑一遍。
3. 从 `RunReport.metrics()` 取：`cost.total_cost`、`cost.tokens_*`、`cost.total_llm_calls`、
   `cost.cost_tasks_unknown`、`counts.unknown_cost_tasks`、`instances[*]` 的逐任务用量。
4. 计算并登记（见 §9 模板）：
   * `c̄`、样本标准差 `s`、标准误 `SE = s/√n`、95% 区间 `c̄ ± t_{0.975,n-1}·SE`
     （n=10 时 `t=2.262`）；
   * `max(c_i)`（重尾时作为保守上界）、费用未知任务数、未知占比；
   * 每个任务的 stop_reason / 官方 verdict，确认没有把 Harness 崩溃算进"便宜"里。
5. 用 `c̄_upper = c̄ + t·SE` 与 `max(c_i)` 两套口径分别算 `estimated_total_cost`，
   取更保守的一个作为预算申请值，并在实验报告中同时写两个数。
6. **不能把 10 题均值当保证**：写清样本量、方差、区间、最坏单题与未知费用比例；
   任何"预计总费用 ¥X"都必须带这些限定语，不能写成承诺。

> 统计口径说明：n=10 的 t 区间建立在近似正态假设上；token 成本通常右偏（少数任务吃满
> 上下文/预算），因此**必须同时报告 max(c_i) 与中位数**，不能只看均值。区间窄不等于
> 下一批一定不超，只说明这 10 个任务之间的离散度。

### 3.5 重试、未知费用与"实际账单 vs 公开单价"

* provider 重试：计入 `llm_calls` 与 token，不计入 Agent 迭代次数；报告中单列重试次数
  （若适配层能区分）或至少用 `llm_calls` 与 `iterations` 的比值提示。
* 未知结果请求：`cost_unknown_calls > 0` → 该任务 `cost=None`，整批总额为**下界**。
  未知任务不得按 0 进入均值；可报告"已知费用任务上的均值"+未知比例，两者一起读。
* 公开单价估算是**记账值**，不等于现金账单：Phase 10 的真实 GLM 批次用预付费资源包，
  记账 ¥1.97880528 并不是当次额外扣款。真实账单必须另外登记（发票/资源包扣减/账单截图
  路径与日期），并给出 `actual / estimated` 差异与原因（缓存价、阶梯价、促销、四舍五入）。
* 换币种：优先用 CNY 计价（BigModel 中国站）。若用 USD provider（Z.AI 等），必须在
  `configs/llm.yaml` 的 `exchange_rates` 里显式声明"1 USD = ? CNY"并记录**可信来源与
  日期**（例如中国外汇交易中心/人民银行授权公布的人民币汇率中间价、服务商账单汇率、
  银行流水汇率）。不允许"缺省按 1.0 处理"进入正式实验预算——那会得到一个没有口径的数。

---

## 4. Docker / 官方 Harness 成本（与 LLM 成本分离）

官方评测的钱不花在 token 上，花在镜像构建、下载、容器执行与磁盘上。两本账必须分列：

| 成本项 | 记录字段 | 说明 |
|---|---|---|
| 镜像构建时间 | 每个 `(repo, version)` 的 build 秒数 | 同 repo 多题共享一次构建；命中缓存为 0 |
| 镜像下载体积 | 拉取字节数 / 镜像层大小 | 冷启动 vs 缓存命中分开记 |
| 镜像缓存命中 | reused_builds / needed_builds | 决定重复批次的边际成本 |
| 每任务执行时长 | 容器运行秒数 + 超时次数 | `timeout_seconds`（默认 1800）之后的处理 |
| 磁盘占用 | 峰值 GB、`disk_free_gb` 预检值 | 镜像按 repo 累积；不足时 fail closed |
| 计算资源费率 | 本地 / 云 VM 每小时或每秒单价 | 本地可记 0 现金成本但必须记墙钟；云按真实单价 |
| 失败重试 | 环境错误重试次数 | 重试成本单列，不摊进成功任务 |

公式（概念式，费率待实测填写）：

```
docker_cost = Σ_build (build_seconds × compute_rate + build_retry_cost)
            + Σ_instance (exec_seconds × compute_rate)
            + storage_gb × storage_rate × duration
            + Σ_pull image_bytes × egress_rate
```

报告要求：

* LLM 成本与 Docker 成本**分别给出**，并注明单位与币种；合并只能发生在"预算申请"这种
  明确加总处，且必须写清合并口径（例如云 VM 按小时计费时，Docker 成本按墙钟时间估算）。
* 本地执行：现金成本可记 0，但**墙钟时间、磁盘峰值、下载体积必须记**，否则"本地免费"
  会掩盖真实瓶颈（Phase 11 的真实阻断就是 C 盘余量约 29GB，不足以批量拉 1GB+ 镜像）。
* 官方 Harness 在原生 Windows 上属 `ENV_ERROR / RESOURCE_BLOCKED`，不算模型失败，
  也不产生可比成本；真实跑批必须在 Linux/WSL2。
* 环境身份：Owner C 的 `SWEBenchEnvironmentIdentity`（instance/env/base image ids +
  environment_hash）应注入 runner 的 `SWEBenchRunConfig.environment_identity`，写进官方
  评测信封；镜像身份变化 → EVALUATION 级恢复拒绝复用旧报告，避免"换镜像后沿用旧结论"。
  官方评测的 `run_id` 以 Owner D 的 `derive_run_id` 为唯一真源（`cp-<patch_set 摘要前
  12 位>-<seed>`，摘要含 `instance_id`），runner 与本计划都不另算一套。

---

## 5. 预算执行

预算不是"跑完看花了多少"，而是派发前就拦住。`SWEBenchRunner` 复用 Phase 10 的
`CostLedger`（先预留、后回填）与 `RepairBudget`：

| 层级 | 上限/机制 | 耗尽行为 |
|---|---|---|
| 单任务迭代 | `RepairBudget.max_iterations=5` | 该任务 `stop_reason=budget_iterations`，**其他任务继续** |
| 单任务测试次数 | `max_test_runs=12` | 同上，归因 `BUDGET_STOPPED` |
| 单任务 token | `max_total_tokens=1_000_000` | 同上 |
| 单任务墙钟 | `max_runtime_seconds=1800` | 同上 |
| 单任务费用 | `max_cost`（按模型设定；无默认值） | 同上 |
| 整批费用 | `max_total_cost` + 每任务 `task_reservation`（必须在构造期同时给正值） | 预留失败 → 不再派发新任务；已完成结果保留；`stop_reason=batch_budget_exhausted` |
| 官方评测 | `timeout_seconds=1800`、`max_workers=4` | 单实例 `ERROR/MISSING`，不冒充模型失败 |
| 官方镜像磁盘 | `disk_limit_gb` + preflight `disk_free_gb` | 不足 → `RESOURCE_BLOCKED`，不进入批量构建 |
| 未知费用保护 | `cost_unknown_calls>0` → `cost=None`；ledger 保留预留 | 整批总额声明为下界；无人值守批次建议未知占比 > 20% 即停止并排查 |

关键语义（与 E-19 实现一致，有测试钉住）：

* **恢复不重复计费**：journal 里已完成的记录直接跳过，不再经过 reserve/settle；
  报告中的记录累计费用只算一次，`ledger.spent` 只反映**本次会话新花**的钱。
* **单任务预算耗尽不停止整批**；只有整批预留失败才停止派发。
* **未知费用不是 0**：未知任务不进均值分母也不记 0，总额标记为下界。
* **中止条件**（工程判断，写入批次日志）：官方 Harness 错误率 > 10%、评测覆盖率 < 90%、
  或出现连续 `WORKSPACE_ERROR/ENV_ERROR` 时停止扩大批次，先修基础设施；不允许"多跑几题
  把成功率拉回来"。

---

## 6. 费用预估表（待校准模板）

在 10 题真实校准完成前，下表的 `c_swe` 保持**未赋值**。禁止用历史 Synthetic 39 题均值
（§8）填入 `c_swe`。校准完成后按 §3.3 填入并重算，保留两位有效数字与区间。

| 规模 | task_count | experiment_groups | seeds | estimated_total_cost 表达式 |
|---|---:|---:|---:|---|
| 1 题 smoke ×2 次 | 1 | 1 | 2 | `2 × c_swe` |
| 10 题校准 | 10 | 1 | 1 | `10 × c_swe` |
| 30 题开发集 | 30 | 1 | 1 | `30 × c_swe` |
| 50 题冻结测试集 | 50 | 1 | 1 | `50 × c_swe` |
| 300 题完整 Lite | 300 | 1 | 1 | `300 × c_swe` |
| 消融（7 组 × 50） | 50 | 7 | 1 | `350 × c_swe` |
| 消融（最低 A/D/G） | 50 | 3 | 1 | `150 × c_swe` |
| 消融（7 组 × 50 × 2 seed，可选） | 50 | 7 | 2 | `700 × c_swe` |

预算申请应使用 `c_swe_upper = c̄ + t·SE`（或 `max(c_i)`，取更保守者），并额外加：

* 10 题校准批次自身：`10 × c_swe_upper`；
* 重跑/失败重试预留（单列，例如 10%–20%）；
* Docker 成本（§4，单独一本账）；
* 汇率/币种换算（如有 USD provider）。

**公式量纲示例（仅演示算术，不是 SWE-bench 预估）**：若某天 `c̄` 被真实校准为 `X` 元，
则 300 题约为 `300X`，7 组消融约为 `350X`。本节故意不填具体数字。

---

## 7. 记录与产物

每次批次必须能回答"这个数字从哪来"：

| 产物 | 内容 | 位置 |
|---|---|---|
| journal | 每个 (instance, seed) 的结论与证据摘要（SQLite，带内容摘要） | `<artifact_root>/journal.sqlite`（由 runner 指定） |
| 现场 manifest | 完整 `RunIdentity`（含 patch_hash）、推理摘要（token/费用/迭代） | `<artifact_root>/runs/<run_id>/swebench_run.json` |
| 补丁 | 模型最终补丁 | `.../model_patch.diff` |
| 官方评测信封 | patch / harness run_id / dataset revision / harness 版本 / config / 环境身份 / 报告哈希 | `.../official_eval.json` |
| 官方报告副本 | 官方 report 的完整副本（哈希写进信封） | `.../official_report.json` |
| 批次报告 | `counts` / `denominators` / `ratios` / `tokens` / `cost` / `runtime` / `instances` | `report.write(dir)` → `swebench-report.json` |
| 实验汇总 | 多批次对照、消融表、费用表 | `docs/experiment-report.md`（Phase 12 E-26） |

报告口径（见 `RunReport.metrics()`）：

* 两个分母同时给：`all_target_tasks`（含预算截停/基础设施故障）与
  `officially_evaluated`（真正拿到官方判定）；
* 官方解决率给端到端与条件两个值，条件值必须与 `evaluation_coverage` 一起读；
* Harness/环境错误留在端到端分母里，但归因为基础设施而不是"模型没修好"；
* 未知费用任务数与 `total_cost_is_lower_bound` 必须与总额同时出现。

---

## 8. 历史 Synthetic 39 题真实数据（仅工程参考）

来源：`docs/notes/phase10.md`（2026-10-18，BigModel `glm-5.3-flash`，seed 0，39 个
**Synthetic** fixture，真实 Docker 评测）。以下数字是从原始 `results.jsonl` / run report
复算的真实值：

| 指标 | 值 |
|---|---|
| 任务数（完成） | 39 / 39 |
| 输入 token | 2,847,185 |
| 其中缓存命中输入 | 1,033,216 |
| 输出 token | 103,568 |
| 总 token | 2,950,753 |
| LLM 请求数 | 434 |
| 记账费用（公开单价估算） | ¥1.97880528 |
| 批次墙钟窗口 | 4,571.45 s |
| 39 条任务 runtime 求和 | 3,835.998 s |
| 独立评测 resolved | 37 / 39 = 94.87% |

派生值（可用于核对公式，但**不得外推**）：

```
mean_cost_per_task     = 1.97880528 / 39 = ¥0.0507386
mean_cost_per_resolved = 1.97880528 / 37 = ¥0.0534812
mean_cost_per_request  = 1.97880528 / 434 = ¥0.0045595
mean_tokens_per_task   = 2,950,753 / 39 = 75,660.3
mean_requests_per_task = 434 / 39 = 11.13
mean_runtime_per_task  = 3,835.998 / 39 = 98.36 s
```

### 使用限制（必须写在任何引用旁）

1. **不是 SWE-bench 均值**：Synthetic fixture 是小型、单文件为主、仓库上下文小；
   SWE-bench 任务的仓库规模、上下文长度、镜像环境、官方 Harness 流程都不同。把
   ¥0.0507/题代入 SWE-bench 预算会严重低估。
2. **不是效果结论**：94.87% 是 Synthetic 上的真实 GLM 结果，不是 SWE-bench Lite 解决率；
   本轮 E-17 真实单题 smoke 仍为 `LIVE NOT RUN`，M3 未达成。
3. **不是现金账单**：该费用按公开单价记账，实际消耗的是预付费资源包（见 §3.5）。
4. 它唯一能证明的是：**这套记录/公式/预算机制能产出可复核的数字**。E-20 的
   `c_swe` 仍必须来自真实 10 题校准。

---

## 9. 10 题校准登记模板

校准完成后把下表填满（每任务一行，数值来自 `RunReport.metrics()["instances"]` 与
逐任务 journal 记录；未知费用填 `unknown`，不要填 0）：

| # | instance_id | repo | input | cached_input | output | requests | retries(可空) | unknown_requests | cost(元) | runtime(s) | iterations | stop_reason | official_verdict |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|
| 1 | | | | | | | | | | | | | |
| … | | | | | | | | | | | | | |
| 10 | | | | | | | | | | | | | |

汇总（必须全部填写，缺项即视为校准未完成）：

| 汇总项 | 值 | 来源 |
|---|---|---|
| 子集 hash / seed | | `subset_hash` / manifest |
| n（费用已知任务数） | | `cost.cost_tasks_recorded` |
| `c̄`（observed_mean_cost_per_task） | | `cost.mean_cost_per_task` |
| 样本标准差 `s` | | 手算/脚本，ddof=1 |
| 95% 区间（t 分布） | | `c̄ ± 2.262·s/√n`（n=10） |
| `max(c_i)` | | 逐任务最大 |
| 未任务数 / 未知占比 | | `cost.cost_tasks_unknown` |
| `c̄_upper`（预算用） | | `max(c̄ + t·SE, ...)` 与 max 取保守 |
| per-resolved / per-request / per-iteration | | §3.3 公式 |
| 实际账单（如有） | | 发票/资源包/账单路径 + 日期 |
| estimated vs actual 差异与原因 | | | |
| 换算汇率（如涉及 USD） | | 来源 + 日期 |

---

## 10. 运行命令（工程模板）

```powershell
# 0) 离线校验（不花钱）：数据集快照、子集、环境 preflight、dry-run 命令拼装
.\.venv\Scripts\python.exe scripts\fetch_swebench_lite.py --check
.\.venv\Scripts\python.exe scripts\swebench_smoke.py --instance-id <id> --split test --dry-run

# 1) 单题 smoke（需要授权 + Docker/Linux 或 WSL2；本轮 NOT RUN）
.\.venv\Scripts\python.exe scripts\swebench_smoke.py --instance-id <id> --split test `
    --provider bigmodel --model glm-5.3-flash --budget 10 --live-llm --repeats 2

# 2) 10 题成本校准（真实调用，需显式授权；本轮 NOT RUN）
#    批次上限与预留必须同时给；未知费用任务会保留预留并把总额标为下界
.\.venv\Scripts\python.exe -m codepilot bench --suite swebench --subset configs\subsets\swebench-lite-dev30.json `
    --max-total-cost <按 c_upper 算> --max-cost-per-task <单题上限>

# 3) 冻结集 / 完整 Lite（Linux/WSL2 + 官方镜像就绪后）
#    300 题前先用 10 题与 50 题批次验证费用与评测覆盖率
```

（CLI 入口以 Lead 最终实现为准；本计划约束的是参数语义：整批上限 + 单任务预留、
子集来自固定文件、是否真实调用必须显式授权。）

---

## 11. 验收与状态

### E-20 已完成（工程实现）

- [x] 规模阶梯 1 / 10 / 30 / 50 / 300 及各自用途、冻结策略（本文 §2）。
- [x] LLM 费用公式（E-20 原文）与派生口径（每任务/每成功/每请求/每轮/每 attempt/
      多 seed/消融），见 §3.3。
- [x] 费用记录字段与报告键，能从 journal 复算（`RunReport.metrics()`，§3.1）。
- [x] 10 题校准流程、统计区间、最坏情形与"不能把均值当保证"的约束（§3.4）。
- [x] Docker / Harness 成本与 LLM 成本分离（§4）。
- [x] 预算执行：单任务/整批/token/迭代/测试/墙钟/未知费用保护（§5）。
- [x] 历史 Synthetic 39 题真实数据的"仅工程参考"标注与使用限制（§8）。
- [x] 校准登记表与记录产物（§7、§9）。

### 待完成（真实校准，NOT RUN）

- [ ] 10 题真实 LLM 成本校准：`c̄`、`s`、95% 区间、`max(c_i)`、未知占比 → 填入 §6/§9。
- [ ] 实际账单 vs 公开单价估算的对账（含资源包/缓存价差异）。
- [ ] Docker 构建/执行成本的实测费率与缓存命中率。
- [ ] 若使用 USD provider：声明可信汇率来源与日期并重算预算。
- [ ] 冻结集 50 题与完整 300 题的官方 Harness 评测（依赖 Linux/WSL2 + 镜像授权）。

**E-20 状态：`ENGINEERING_IMPLEMENTED / REAL_COST_CALIBRATION_PENDING`。**
在上述 PENDING 项完成前，本文不提供任何 SWE-bench 费用承诺，也不把 Synthetic 39 题
数字当成 SWE-bench 均值。
