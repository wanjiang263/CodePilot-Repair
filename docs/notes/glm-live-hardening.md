# GLM 真实实验暴露缺陷的专项修复（Harness Hardening）

分支 `codex/glm-live-hardening`，基线 HEAD `b9848f72d7ea`（`codex/glm-live-m2-single-task`）。

本轮不开发新 Phase，也不执行任何真实模型实验：**0 次付费 API 调用**。
第一次真实 GLM 单任务实验的原始证据留在
`artifacts/phase10/glm-live-m2-cda534ca9b4b/`，未被覆盖、未被改写。

---

## 1. 准确根因

那次运行共 7 次 HTTP 200、¥0.1334616，最终没有任何补丁，报告写着
`evaluation_status=NOT_RESOLVED`、`resolved=false`、`environment_failures=0`、
`stop_reason=workflow_error`，`error_details=["plan: PathAccessError: path already exists: tests/test_ledger.py"]`。

链条是这样的：

1. GLM 产出的 `RepairPlan` 形状合法，但 `tests_to_run` 里写着
   `planned:tests/test_ledger.py::test_new` ——「我打算写一个还不存在的测试」。
2. `Planner._validate_plan` 把 `planned:` 前缀当成"待创建文件"，用
   `policy.resolve(..., must_exist=False)` 去校验，而那个文件**已经存在**，
   于是 `WorkspacePathPolicy.resolve` 抛 `PathAccessError("already_exists")`。
3. `graph/builder._guard` 的 `except Exception` 把任何异常一律写成
   `RunStatus.ERROR` + `StopReason.WORKFLOW_ERROR`。
4. 图直接跳到 finalize，CodingAgent / PatchValidator / RegressionRunner /
   Reviewer 一个都没执行，补丁为空。
5. runner 仍然把这份空补丁送进真实 Docker 独立评测（gold 套件测的是坏掉的基线，
   不是模型的补丁），得到 `NOT_RESOLVED`；`metrics` 把它计入模型未解决的分母，
   并且因为 `environment_failure` 只看 `INFERENCE_ERROR`/`EVALUATION_ERROR`，
   `environment_failures` 报 0。

一句话：**一次本可以纠正的模型字段错误，被 Harness 变成了一次崩溃，又被评测层
记成了模型修复失败。**

同时暴露的观测缺口：`events.jsonl` 16 条事件的 `usage` 全为 `null`
（`LLMRegistry` 从没拿到 `EventSink`）；`Usage.by_model` 只有一个聚合项，没有角色
维度；被拒的计划与 `Planner.tool_records` 根本没落盘，事后无从复盘模型当时写了什么。

---

## 2. Planner 语义纠正的完整流程

```text
GLM / FakeLLM
   ↓  只读工具环（read_file / find_symbol / …，每条都进审计）
Pydantic Schema 校验  ←─┐
   ↓                    │ 同一个有界纠正环
语义 + 权限校验          │ max_attempts = 3(SCHEMA) + 2(语义纠正)
   ↓ INVALID            │
带字段路径与"该改成什么"的纠正反馈
   ↓ VALID ─────────────┘
Planner 成功 → generate_patch
耗尽 → RunStatus.PLAN_REJECTED / StopReason.PLAN_REJECTED
        error_class=plan_semantic_invalid, error_origin=model
```

关键实现点：

- **语义校验进了纠正环内部**。`complete_structured(..., semantic_validator=...)`
  只把 `AgentFailure.retryable` 为真的错误转成一次纠正；权限拒绝、服务商故障、
  程序异常一律直接冒到节点边界，不再被伪装成"再问一次模型"的机会。
- 纠正反馈带 `field`（如 `tests_to_run[0]`）、`code`（如
  `intent_marker_in_tests_to_run`）、`reason` 与 `hint`，并解释"可执行测试"与
  "计划新增测试"的区别。上一轮输出按 `_PREVIOUS_LIMIT` 截断后过 `redact_text`，
  不无条件重复整段上下文。
- **计数分开**：`LLMRequest.attempt` / `correction` 记录模型被重新问了几次；
  服务商 HTTP 重试次数仍只在 `OpenAICompatibleProvider.attempts` / `unacknowledged`
  里。`RepairState.plan_corrections` 只累计前者。
- 被拒的计划连同字段错误写入 `plan_rejections.jsonl`（脱敏）。首轮实验什么都没留下，
  这份证据现在是有的。
- System prompt 里写明了测试契约：`tests_to_run` 只接受**已存在**且被授权执行的
  路径/NodeID；要新增的测试写进新字段 `planned_new_tests`；已有测试文件受保护；
  没有可信测试时留空并说明；模型不能自行授权。

### `planned:` 的合法与非法边界

| 输入 | 结果 |
|---|---|
| `tests/test_ledger.py`（已存在） | 接受 |
| `tests/test_ledger.py::test_add_positive`（函数已在文件里） | 接受 |
| `tests/test_param.py::test_add[1]`（参数化 id） | 接受：id 由收集期生成，静态核对不了，误判会拒掉合法计划 |
| `tests/test_ledger.py::test_new`（文件在、函数不在） | 拒：`test_case_not_found` |
| `planned:tests/test_ledger.py::test_new`（文件在） | 拒：`intent_marker_in_tests_to_run`，且**不**触碰文件创建校验 |
| `planned:tests/test_new_file.py::test_new`（文件不在） | 同上，仍按非法标记处理 |
| `planned_new_tests=[tests/test_extra.py::test_new]` + 可信授权 | 接受，但绝不进入可执行目标 |
| 同上 + 无可信授权 | 拒：`unauthorized_new_test` |
| `planned_new_tests` 非空而 `tests_to_run` 为空 | 拒：`new_tests_are_not_sole_evidence` |
| `planned_new_tests` 指向已存在的测试文件 | 拒：`existing_test_is_protected`（即使已授权新增） |
| `../outside/test_x.py` / 符号链接逃逸 / `private-eval/…` | 拒：`path_escape` / `forbidden_path` |

把 `planned:` 从前缀里 `removeprefix` 掉**不是**修复：那样 `tests/test_ledger.py::test_new`
照样是"函数不存在"，所以它被 `test_case_not_found` 拒绝。契约的语义而不是字符串形态
才是校验对象。

### 现有测试保护保持不变

- `WorkspacePathPolicy` 的默认拒绝名单、复现目录凭证（`repro_write`）与
  `allow_new_tests` 的判定分支**没有放宽**；计划层现在只是**更早**拒绝未授权意图，
  把原来要等到 Coding Agent 写文件时才暴露的问题前移了。
- `PatchValidator`、`patch/diff.py` 的交付门（`allow_new_tests and new_tests_reviewed`）、
  ToolRegistry 的角色权限、`FailureSignature` 全部未改。
- Phase 9 M2 验收语义不变：`provisionally_verified` 的含义、`finalize` 的保守判定、
  回归与复审的执行路径都照旧；本轮新增的终态只有 `plan_rejected`。

---

## 3. 异常如何进入 RunReport 与评测

`codepilot/errors.py` 是唯一翻译点：`classify_exception(exc, node=...)` 产出
`AgentFailureError`，携带 `failure_class` / `origin` / `field_errors` /
`corrections_used` / `details`。

| FailureClass | Origin | 图终态 | 评测归因 |
|---|---|---|---|
| `plan_semantic_invalid` | model | `plan_rejected` | `MODEL_INVALID_PLAN` |
| `model_output_invalid`（Schema 连续失败、请求了不存在的工具） | model | plan 节点为 `plan_rejected`，其余 `error`/`workflow_error` | `MODEL_OUTPUT_ERROR` |
| `tool_permission_denied` | tool | `error`/`workflow_error` | `HARNESS_ERROR` |
| `workspace_error` | workspace | `error`/`workflow_error` | `HARNESS_ERROR` |
| `llm_provider_error` | provider | `error`/`workflow_error` | `HARNESS_ERROR` |
| `llm_request_uncertain` | provider | `error`/`workflow_error` | `HARNESS_ERROR` |
| `harness_internal_error`（未识别，默认） | harness | `error`/`workflow_error` | `HARNESS_ERROR` |

三条边界：

1. **真正的程序异常仍是 `workflow_error`**：默认分类是 `HARNESS_INTERNAL_ERROR`，
   不做"宽泛捕获后算成模型错误"的反向掩盖。
2. `error_class` / `error_origin` 写进 `RepairState`（带默认值的可选字段，
   `CHECKPOINT_SCHEMA_VERSION` 不用 +1）、`report.json`、`events.jsonl` 的 `extra`、
   以及 `results.jsonl` 的 `attribution` / `error_class` / `error_origin`。
3. 归因判定看 `error_class`，不再只看 `stop_reason`——这是"模型给出一个可纠正的
   字段错误却被归因为 Harness 崩溃"不可能再复现的原因。

---

## 4. 逐请求与逐角色可观测性

新增 `codepilot/llm/calls.py`：`LLMRunContext`（run/task/node/iteration）与
`CallRecorder`（`llm_requests.jsonl`）。每次**实际发出**的模型请求落一条元数据：

`call_id`、`run_id`、`task_id`、`seq`、`role`、`node`、`iteration`、`purpose`、
`provider`、`model`、`attempt`、`correction`、`status(ok|error|uncertain)`、
`finish_reason`、`input/output/cached/total_tokens`、`cost`、`cost_known`、
`currency`、`error_type`、`http_status`、`tool_calls`、`duration_ms`。

- 所有角色仍走同一个 `LLMProvider` 链，没有为 GLM 开特殊分支。
- 绑定点：`InferenceAssembly.run()` 里 `ProviderBundle.bind_run(...)`。此前真实运行
  的 provider 链从来没拿到 `EventSink`，所以 `events.jsonl` 的 usage 全是 `null`；
  注册表因此新增 `LLMRegistry.bind_run(...)`（run 身份、事件流、录制落点晚绑定）。
- `settings.record_llm` 现在真的接进来了：`ProviderBundle.live(..., record_llm=True)`
  → `LLMRegistry(mode=RECORD)`，`llm_calls.jsonl` 落在每个 run 自己的目录里。
  未绑定落点时 `RecordingLLM.complete` **拒绝发请求**——不产生"花了钱但没录制"的调用。
- `Usage` 增加 `by_role`、`cached_input_tokens`、`cost_unknown_calls`、`currency`，
  以及 `role_totals_reconcile()` 自检：角色视图、模型视图与总账必须同账。
  币种不同的调用直接相加会抛错，而不是悄悄混算。
- 恢复不重复记账：`Deps.restore_usage` 改用 `Usage.reset_to`，逐字段覆盖，
  新增的 `by_role`/未知费用计数不会因为只恢复四个总数而与总账脱节。
- 默认**不**保存：API key、Authorization、原始 prompt、未脱敏工具参数、
  `reasoning_content`、隐藏测试内容、gold patch。逐请求日志只有元数据；完整请求体
  只在显式 `record_llm` 时写，并且照旧过 `redact()`。

### Planner 工具调用审计

`schemas/audit.py`：`PlannerToolRecord`（`tool_call_id`、`name`、`role`、`permission`、
`status(succeeded|denied|failed)`、脱敏 `arguments_summary`、`error_code`、
`duration_ms`、`iteration`、对应模型请求 `request_id`）与 `PlannerToolAudit`。

四种状态必须区分开，靠的是记录本身而不是计数：

| 情况 | 记录 |
|---|---|
| 模型没请求工具 | `audit.requested == 0` 且 `records_present is True`，`plan_tools.json` 仍然落盘 |
| 请求了但被拒 | `status=denied`，计入 `requested_but_denied` |
| 工具执行成功 | `status=succeeded` |
| 审计没采集到 | `RepairState.plan_tools is None`，报告写 `NOT COLLECTED (not the same as zero calls)` |

`denied` 与 `failed` 的区分靠 error code：`Tool.execute` 会把 `PathAccessError` 翻译成
失败响应而不抛出，所以只能在读 error code 的地方判权限拒绝（`path_escape`、
`forbidden_path`、`identity_changed`、`unauthorized_test`、`unregistered_tool`）。

审计同时进三处：`events.jsonl` 的 `tool_call` 事件、`RepairState.plan_tools`、
run 目录的 `plan_tools.json`。

---

## 5. Benchmark 归因与指标口径

新增 `codepilot/eval/attribution.py`：`Attribution`（10 类）、`EvaluationState`
（evaluated / not_run / error）与纯函数 `attribute_run(...)`。判定顺序先问
"派发/执行/评测哪一层断了"，再问结论是什么。

`codepilot/eval/runner.py`：

- **空补丁不做独立评测**。没有补丁就没有"模型的补丁"可测，跑 gold 套件测的是坏掉的
  基线，还要花一次容器时间。`resolved` 因此是 `None`（未判定）而不是 `False`。
- `ExecutionState`（派发/执行）与 `evaluation_state`（评测是否发生）与
  `attribution`（谁错了）三个维度分开。
- `record.effectively_evaluated` 与 `record.counted` 分开：外层 `completed` 不再被
  当成"有效评测过"。
- 旧格式（v1）只读时映射，不回写历史：`TaskRecord.from_dict` 发现缺
  `record_version` 就按 `empty_patch` / `evaluation_status` / `stop_reason` 推出
  归因。那次真实 GLM 崩溃的 v1 行由此从"模型未解决"映射成 `HARNESS_ERROR` +
  `evaluation_state=not_run`，而历史产物本身一个字都没改。
- `results.jsonl` 新增带默认值的字段，`metrics.json` / `manifest.json`
  的 `schema_version` 升到 2。

`codepilot/eval/metrics.py` 新增 `attribution_metrics(...)`，报告同时给出：

- 端到端解决率：分母 = 全部派发（**含** Harness 错误、超预算、未评测的任务）；
- 条件解决率：分母 = 真正拿到独立评测判定的任务；
- 有效评测覆盖率；
- Harness / 环境 / 沙箱错误率（三者分开，不再混为一谈）；
- 模型计划拒绝率与模型输出错误率。

既有 `resolved_rate` / `first_attempt_resolved` / `repair_success_rate` /
`self_verified_precision` 的数学定义与分母口径**没有改动**：Harness 错误仍然留在
总体分母里，所以不可能靠排除崩溃来偷偷把成功率做高。

---

## 6. 节点级 Checkpoint 接入

`codepilot/eval/assembly.py`：

- `InferenceAssembly(checkpoint=True)` 用 Phase 9 现成的 `open_checkpointer` /
  `run_with_checkpoint` / `resume_run`，没有新建持久化框架。
- 每个 (task, seed) 一套独立 run 目录 + `thread_id = run_id`（run_id 里已带
  task、seed 与身份前缀），checkpoint 落在 `runs/<run_id>/checkpoints.sqlite`。
- 续跑时 `BenchmarkRunner` 从 `manifest.json` **继承原 run_id**，且只在配置指纹一致时
  继承——否则每次都开一块新现场，节点级恢复永远用不上。
- 身份绑定：首次运行把 `workspace_identity.json`（workspace 路径、source、
  base commit、`repository_id`）写进 run 目录；恢复进程**读它**而不是重新推导 id。
  实测原因：重新推导一旦与创建现场不一致，恢复进程拿到的是零符号索引，症状是
  "计划里的 symbol 不在索引里"，看起来像模型乱写。
- 索引改为在跑图**之前**提交。与图共用一个 session 时，进程被硬杀会让索引库停在
  未提交状态，恢复进程面对一个空索引。
- 与任务级 journal 协调：journal 里已有结论的任务直接复用、不重跑；只有没有结论的
  任务才进入图，并优先从 checkpoint 接上。

**不宣称 exactly-once。** 三个受控崩溃点都用独立进程（``os._exit``）实测过：

| 崩溃点 | 恢复行为 | 证明的东西 |
|---|---|---|
| plan 节点要发第一次模型请求之前 | 从 plan 继续并跑完整张图 | 已完成的 localize / impact / build_env 不重跑 |
| 目标测试**已成功返回、checkpoint 尚未写入** | 该轮重做并**重新计费** | 没有两阶段提交就不能宣称 exactly-once；口径是 at-most-once-with-verified-evidence |
| 复审的外部操作「意图已写入、结果未提交」 | **拒绝**继续，写 `resume refused`，归因 `HARNESS_ERROR` | 状态不明的外部操作绝不盲目重放 |

另有两条恢复前提被测到：workspace / base / ``repository_id`` 与 checkpoint 不一致时
拒绝复用现场；续跑只在配置指纹一致时才继承原 ``run_id``。

---

## 7. 测试与门禁

本轮新增/扩充离线测试 73 项（全部 Mock/FakeLLM/FakeSandbox，零网络、零容器、零费用）：

- `tests/unit/agents/test_planner_semantics.py`（29 项，新增）：§2 表中全部合法/非法
  边界、纠正成功、纠正预算耗尽、无越权自授权、真实 GLM 的 `planned:` 脱敏复现。
- `tests/unit/llm/test_llm_observability.py`（12 项，新增）：逐请求记录、角色聚合与总账
  一致、缓存命中与 `finish_reason`、未知费用不记为 0、失败与结果未知的区分、
  币种不混算、日志不含 prompt 与密钥、Planner 多轮只读工具端到端审计与"被拒"分类、
  `record_llm` → RECORD 模式。
- `tests/eval/test_attribution.py`（12 项，新增）：九种结局归到九个不同类别、
  Harness ≠ ENV ≠ 模型失败、两套解决率与覆盖率的分子分母、v1 记录映射、
  runner 端到端产物、`claims_model_capability` 的两个前提。
- `tests/eval/test_bench_checkpoint.py`（9 项，新增）+ `_bench_checkpoint_worker.py`：
  上表三个独立进程崩溃点的恢复行为、已 checkpoint 的外部操作不重跑、悬挂意图拒绝重放、
  身份/base 不符拒绝复用、run_id 继承的配置一致性判定。
- `tests/graph/test_glm_live_hardening.py`（4 项，新增）：真实状态机里的纠正成功、
  纠正耗尽 → `plan_rejected`、越权工具请求不算崩溃、合法计划的完整 M2 闭环无回归。
- `tests/unit/llm/test_structured.py`（+5）：语义阶段与 Schema 阶段的归因区分、
  不可纠正错误不被转成纠正机会、attempt/correction 传到 provider。

最终门禁（四条命令全部 exit 0）：

```text
ruff check .            All checks passed!
ruff format --check .   347 files already formatted
mypy src                Success: no issues found in 160 source files
pytest -m "not docker and not llm" --cov --cov-report=term-missing
    2879 passed / 1 skipped / 44 deselected，覆盖率 92.67%，1546.12s，exit 0
```

与本轮开始前基线的对照：2806 passed → 2879 passed，净增 73 项 =
5 个新测试文件的 66 项（29 + 12 + 12 + 9 + 4）+ 既有文件扩充的 7 项
（`test_structured.py` 的语义阶段用例、`test_registry.py` 把"record 模式必须构造期
给路径"一条拆成"晚绑定可用 / 未绑定拒发"两条）。覆盖率 92.72% → 92.67%，
`fail_under=80` 未下调。没有为了通过而新增 skip，也没有弱化任何既有断言；
skip 数与基线相同（1 项 embedding 用例，按既有条件跳过）。

Sandbox 核心代码未改动，因此未重跑全部 Docker 安全测试。


---

## 8. 剩余风险

**P1**

- 归因机制从未在真实模型上验证过。离线证明的是"给定这些错误形态会归到这些类"，
  不是"真实 GLM 只会犯这些错"。第二次真实单任务实验必须核对归因是否落在预期类别。
- `run_regression` 仍然会对 baseline 报告过、但工作区里已不存在的测试路径抛
  `PathAccessError`。本轮把它正确归因成 `workspace_error`（不再伪装成模型失败），
  但没有改动回归节点的容错策略——那属于 Phase 9 的范围，未在本轮扩大。
- `plan_corrections` 的上限（Schema 3 + 语义 2）是本地调参，没有真实模型数据支撑。
  真机上"模型能不能改对"目前只有 1 次实验、0 次纠正机会的证据量。

**P2**

- 节点级 checkpoint 默认开启，会给每个 (task, seed) 多一个 SQLite 文件与
  `durability="sync"` 的写放大；39 题 × 3 seed 的批跑成本未实测。
- `llm_requests.jsonl` 的 `seq` 在恢复进程里从已有行数续号，但如果同一个 run 目录被
  人为复制过，序号会失真。它用于定位与对账，不作为唯一凭证。
- DA-015 / DA-016（本轮新增，见 `docs/05-精细化待办清单.md`）仍是效果证据缺口。

---

## 9. 是否具备第二次真实 GLM 单任务实验的条件

具备，且需要它才能关闭的正是本轮测不了的那部分：

1. 计划语义不合法时，模型**能不能在 2 次纠正内改对**——这是真实模型行为问题。
2. 逐请求/逐角色账目在真机上是否与总账一致（真实 provider 会回传 cached token、
   `finish_reason`、未知费用等组合）。
3. Planner 的只读工具多轮调用在真实模型上是否真的被审计到。
4. `plan_rejected` / `HARNESS_ERROR` 的归因分布是否符合预期。

建议的第二次实验口径与第一次一致（同一个 fixture、同一份配置指纹、真实 Docker 评测、
`--allow-live-llm` 显式授权、整批费用上限与每任务预留额都设好），并额外打开
`--record-llm` 留下脱敏后的完整请求体。跑完先看归因与 `plan_rejections.jsonl`，
再谈模型修复能力结论。

**本轮结束后没有发起任何真实 API 请求。**
