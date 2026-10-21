# Phase 12：预算感知的闭环准入与最终验收收口

本轮日期 2026-10-20 夜间开工、2026-10-21 收口。分支 `codex/phase11-runtime-acceptance`，
基线 HEAD `55a4380`，开工快照与 diff 备份在 `D:/QoderSnapshots/p11final-20261020/`
（工作树按原样保留，未做 reset/stash/清理）。

本轮**只做工程开发与离线/真实容器验证**：0 次真实 GLM 或其他付费模型调用、0 个新的
SWE-bench 真实评测、未运行 test50/test300 或任何批量。验证走 FakeLLM、确定性夹具与
真实 Docker daemon。承接
[Phase 11 闭环与预算记录](phase11-closure-budget.md) 的 §9–§13，不重复审视已完成的工作。

---

## 1. DA-024：先把 glm14 那 12 次额度算清楚

第三次真实单题（`artifacts/phase11-glm14-20261020/`）的内部终态是
`budget_exhausted` + `stop_reason=budget_test_runs`，而费用只花了 ¥0.192/¥10。逐次核对
`state_final.json` 与 `events.jsonl` 里四条 `budget_check` 读数，额度的去向是确定的：

| 时点（事件 seq） | 该判定读到的 `test_runs/上限` | 之间发生了什么 |
| --- | --- | --- |
| `build_env`（11） | `0/12` | 基线两次全量 → 2 |
| 复现验证 | — | 复现测试在 base 上跑一次 → 3 |
| 第一轮编码工具的 `run_tests` | — | 模型自己跑两次 → 5 |
| `run_target_tests`（38） | `5/12` | 目标测试一次 → 6 |
| `run_regression`（41） | `6/12` | L2 + L3 各一次 → 8（`complete=true`，779 条） |
| `repair_patch`（49） | `8/12` | **审查给 high → 启动第二轮**；编码又自跑两次 → 10 |
| `run_target_tests`（61） | `10/12` | 目标测试一次 → 11 |
| `run_regression`（64） | `11/12` | L2 一次 → 12；**L3 在节点入口被 `budget_test_runs` 拦下** |

结论不是"12 太小"。一版候选补丁的闭环需求是**固定三项**：目标测试 1 次 + 回归 L2 1 次 +
回归 L3 1 次 = **3 次容器调用**（计数口径是 `AccountingSandbox` 数的 `run_tests` **调用次数**，
与这次圈进 1 条还是 779 条无关）。两轮就是 6 次，加基线 2 次、复现 1 次，共 9 次 < 12。
真正越界的是**编码阶段自己调用的 `run_tests`**：glm14 里两轮共 4 次，恰好把最后那 3 次挤掉。
而既有判定的作用点在"花完之后"——测试节点入口只能宣布额度没了，无法阻止一轮**注定验不完**
的修复被启动。

## 2. 机制：闭环准入 + 本轮份额

实现落在预算层与节点层各一处，不新增第二套算钱逻辑：

- **算式**：`BudgetManager.closure_test_plan(state, *, target_tests_planned,
  impact_level_expected, full_level_expected, runs_spent)`（`graph/budget.py`，
  与 `closure_reservation` 同一节）。产出 `ClosureTestPlan`：
  `required = 补丁校验 0 + 目标测试 + L2 + L3`、`remaining = max_test_runs - 已花`、
  `feasible = remaining ≥ required`、`coder_share = max(remaining - required - 2, 0)`。
  四个算术常数 `CLOSURE_PATCH_VALIDATION_RUNS / CLOSURE_TARGET_TEST_RUNS /
  CLOSURE_REGRESSION_LEVEL_RUNS / CLOSURE_FLAKY_RETRY_RESERVE` 是具名的，口径直接对着
  `run_target_tests` 与 `RegressionRunner` 里真正会发生的调用。
- **判据与回归同源**：`regression.py` 新增 `usable_baseline(state)` 与
  `full_regression_runs(state)` / `MAX_FULL_REGRESSION_RUNS`，回归执行器与准入判定问的是
  同一个问题。抄第二份判据迟早会漂，漂的结果是"准入以为要跑、实际不跑"，凭空多留一次额度。
- **作用点一：拒绝启动**（`nodes._closure_refused`，在 `repair_patch` 入口、既有
  `Activity.REPAIR` 守卫之后）。不足时写 `status=budget_exhausted` +
  `stop_reason=budget_closure_test_runs`，**不回退 Workspace、不重新规划、不派发编码**，
  而且顺序刻意排在"清空 `regression` / `review`"之前 —— 已经挣到的补丁、审查结论与回归
  证据原样留在 state 与报告里。
- **作用点二：份额**（`nodes._coding_stage` → `CodingAgent.run(test_run_ceiling=…)`）。
  与 Phase 11 的 `token_ceiling` / `cost_ceiling` 同一条纪律：**只收紧，不放松**。循环内
  逐次判定，超出份额的 `run_tests` 请求被回话拒绝（`error_code=closure_reserved`），其余
  工具照常可用；只有**执行成功**的 `run_tests` 才计入份额，失败的尝试没有花容器次数。
  不变量：`coder_share + required ≤ remaining`，于是一轮修复结束后的累计次数不会越过
  `max_test_runs - required`，闭环那几次物理上还在。

三条刻意保留的判断：

1. **flaky 复跑预留（2 次）不参与准入判定**，只从编码份额里扣。它仅在真的出现疑似新失败时
   发生；算进门槛会把"本来能走完闭环的一轮"判成不可行。它拿不到额度时的后果是既有的安全
   降级（`complete=false` → 审查如实记未执行），不会被读成通过。
2. **准入不足绝不是降级覆盖面的理由**：`sampled` 与 `full` 都是同一次调用，所以省额度只能
   靠"不启动那一轮"，不存在把 FULL 悄悄改成 SAMPLED、跳过必要测试、跳过审查或放宽判定。
3. **没有上调 `max_test_runs` 默认值**（仍是 12，`configs/full.yaml` 同）。§1 的算术说明
   缺陷不是额度数值：真实需求 9 次 < 12 次，越界的是消耗顺序。改默认值只会把同一件事推到
   更远的地方，还会让"额度是拍脑袋来的"这个毛病继续留在配置里。想跑两轮以上完整闭环的
   用法应当显式调大 `max_test_runs`（每多一轮 +3 次，另加模型自跑的份额）。

## 3. 状态机、Schema、事件与报告的一致性

| 面 | 变化 |
| --- | --- |
| 状态机 | `repair_patch` 入口新增一条终态边：`check_budget → repair_patch → finalize`（`budget_closure_test_runs`）。路由函数未改，`terminated()` 自然把它短路到 finalize |
| 停止原因 | `StopReason.BUDGET_CLOSURE_TEST_RUNS = "budget_closure_test_runs"`。留在 `budget_*` 家族里，这样既有的归因分桶（`TaskOutcome.over_budget`、SWE-bench 的 `BUDGET_STOPPED`、`attribution.py` 的 `budget_stopped`）会继续把它算成预算停止，而不是掉进“补丁无效/模型没修好”；值本身与 `budget_test_runs` 区分：`budget_test_runs` 是“12 次已经花完”（运行结果），这一条是“还没花但已算出来不够”（派发前的算术），混在一起会把准入判定读成消耗殆尽 |
| RunStatus | 复用既有 `budget_exhausted`（类型化状态不新增），区分口径全部落在 `stop_reason` 上 |
| Schema | `RepairState.closure_admission: ClosureAdmission | None = None`（带默认值的可选字段，历史 checkpoint 照旧可读，`CHECKPOINT_SCHEMA_VERSION` 不需要 +1）；`schemas/budget.py:ClosureAdmission` 记录 `feasible / runs_spent / allowance / required / 各层级 / retry_reserve / coder_share / trigger / detail` |
| 事件 | 每次编码派发写一条 `budget_check`（`status=ok|insufficient`，`extra.closure_test_runs` 带整套读数）；拒绝时再写一条 `status=refused`。报告、事件与 `failure_reasons` 用的是**同一句** `ClosureTestPlan.detail()` |
| 报告 | `VerificationClosure.closure_admission`；`feasible=false` 时 `unexecuted_stages` 追加 `repair_round:<trigger>:…`；markdown 新增 `Closure admission (test runs)` 一行，被拒时写 `REFUSED before dispatch`。`results.jsonl` 通过既有 `stop_reason` 字段直接携带新原因，无需改评测侧 schema |
| 成功判定 | `verification_level()` 未放松：`m2_verified` 仍要求目标测试 PASS + 回归 complete + 审查绑定当前候选补丁。`budget_exhausted` 这一档从来不在成功集合里，`_CLAIM_STATUSES` 也不含它，因此测试额度不足不可能被报成"测试通过/模型修复成功/官方评测成功" |

## 4. 测试证据（DA-024）

离线图级（真实 git 夹具 + 真实 PatchValidator/RegressionRunner/PatchReviewer，FakeLLM +
脚本化 Sandbox）：`tests/graph/test_phase12_closure_admission.py`

| 用例 | 守住的任务书条目 |
| --- | --- |
| `test_a_second_round_that_cannot_finish_its_closure_is_never_started` | §2.1.1/2.1.3/2.2：剩余 2 而闭环要 3 → 拒绝启动；零新增编码调用、零新增容器、审查只问一次；`attempts` 仍为 1、`review_repairs=0` |
| `test_the_refused_round_keeps_the_reviewed_patch_and_its_evidence` | §2.1.3：原补丁、审查的 high 问题、回归与目标测试证据全部保留；markdown 里能同时读到 `REFUSED before dispatch` 与审查意见原文 |
| `test_refusal_is_idempotent_under_replay_and_recovery` | §2.3.5：同一条边重放与"新进程重新播种计数器"都给出逐字相同的判定，容器次数与账本调用数一动不动 |
| `test_a_round_with_room_completes_repair_tests_regression_and_review` | §2.3.1 + 边界放行侧：`remaining == required == 3` 时修复→校验→测试→回归→复审全部发生，`m2_verified` 成立且审查绑定第二版补丁 |
| `test_the_allowance_never_buys_a_quieter_regression` | §2.1.4：放行那一轮的回归仍是 `full`，`sampled_ratio/seed` 未被写过，L1/L2/L3 三层齐 |
| `test_the_boundary_is_the_exact_number_of_runs_the_closure_needs` | §2.3.3：节点级两侧——`spent+3` 放行、`spent+2` 拒绝，且拒绝侧 coder 调用数与容器次数都不变 |
| `test_the_coding_stage_cannot_borrow_the_runs_reserved_for_its_own_closure` | §2.1.2：份额 2 而模型请求 4 次 → 只有 2 次进沙箱，2 次回话 `closure_reserved`，闭环那 3 次照旧完成并拿到 `m2_verified` |
| `test_a_local_tool_cap_never_overrides_the_global_stop_reason` | §三.3（DA-022 侧）：30 次工具上限用满 + `ENV_ERROR` 时，`coding_stop_reason=budget_tool_calls` 与 `stop_reason=sandbox_failure` 各写各的，审查未被调用 |
| `test_an_unverified_candidate_from_a_refused_round_cannot_claim_m2` | §2.2：级别 `budget_exhausted`、`budget_usage.test_runs="5/7"`、事件流与报告同读数 |
| `test_a_succeeded_status_alone_never_buys_m2`（3 参数） | §2.3.6：回归未完成 / 审查未执行 / 审查绑的是另一版补丁，三种情形都从 `m2_verified` 落回 `provisionally_verified` |
| `test_a_plan_is_a_reading_not_a_ledger` | §2.1 判定是纯读数：同一 plan 两次 record 相等，不产生用量 |

单元层：`tests/unit/graph/test_closure_budget.py` 新增 10 项（调用口径、边界恰好/少一、
flaky 预留只从份额扣、L3 只在执行器判不跑时才不计、不变量扫描 0…13、负计数即配置错误、
`record`/`detail`/`summary` 同一算式、可行侧写明份额、判定不触碰账本）。
工具层：`tests/unit/agents/test_coder.py` 新增 4 项（份额内才执行、份额 0 一次都不跑、
不传份额行为不变、失败的调用不吃份额）。

真实 Docker：`tests/graph/test_phase12_closure_admission_docker.py` 两项——拒绝侧在真容器上
"一次容器都不再多跑"，放行侧在真容器上把第二轮的目标测试/L2/L3/复审全部走完。
**执行状态见 §7 第 5 行：两项都已在真实 daemon 上实跑通过**（拒绝侧"一次容器都不再多跑"、
放行侧"第二轮在真容器里走完目标测试/L2/L3/复审"）。

## 5. DA-022：工程验证与真实模型状态

核对结论：Phase 11 已有的用例覆盖任务书 §三.1 列出的每一环，本轮不重复实现。

| 要求的环节 | 已有证据 |
| --- | ---|
| 30 次工具上限 + 有效补丁 + 冻结候选 | `tests/graph/test_phase11_closure.py::test_coder_tool_cap_with_valid_patch_still_runs_the_whole_closure`（离线）、`tests/graph/test_phase11_closure_docker.py::test_local_tool_cap_closure_completes_on_real_containers`（真实容器，含 `frozen_by_coding_stage` 与 JUnit 落盘断言） |
| PatchValidator → 目标测试 → 回归 → Reviewer → 报告 | 同上两条：`closure` 逐阶段断言 + `unexecuted_stages == []` + `verification_level == m2_verified` |
| 无补丁路径 | `test_tool_cap_without_any_patch_terminates_without_a_success_claim`（`EMPTY_PATCH`、审查 NOT EXECUTED） |
| 无效补丁路径 | `test_tool_cap_with_a_patch_the_validator_rejects_does_not_finish_successful` |
| 硬预算耗尽路径 | `test_path_budget.py` 六类各一条 + `test_budget_stop_still_writes_the_run_report` |
| 局部停止不覆盖全局停止 | 本轮新增 `test_a_local_tool_cap_never_overrides_the_global_stop_reason`（工具上限与 `sandbox_failure` 同时存在时各写各的）；既有 `test_local_tool_exhaustion_does_not_consume_the_review_allowance` 守额度独立 |
| 审查结论绑定被冻结的候选 | `test_a_review_of_an_older_patch_never_blesses_the_new_candidate` + docker 侧 `review.reviewed_patch_hash == patch_hash(current_patch)` |
| 真实容器里测试失败不得混进审查 | `test_real_container_failure_still_refuses_to_bless_the_candidate`（负控制） |

**真实模型同形状状态：`REAL SAME-SHAPE NOT OBSERVED`（维持不变）。** 三次真实单题
（glm12/glm13/glm14）编码阶段分别是 5、5、10 次工具调用，从未触到每轮 30 次上限，
`coding_stop_reason` 一直为 `None`。本轮没有发起任何付费调用，也没有伪造日志、强迫模型
空转或改写历史产物来"制造"该形状。

未来经明确授权后的一次真实单题验证（**只跑一次，未触发目标形状不得自动重试**）：

```bash
# WSL2 Ubuntu-24.04，/root/cp-run/CodePilot-Repair（沿用 Phase 11 的 base-only 浅镜像站点通道）
DOCKER_HOST=unix:///mnt/wsl/docker-desktop/shared-sockets/guest-services/docker.proxy.sock \
CODEPILOT_DISPATCH_BUDGET_CNY=10 \
CODEPILOT_DISPATCH_LEDGER=<新站点>/dispatch-ledger.json \
.venv/bin/codepilot bench-swebench \
  --instance-id marshmallow-code__marshmallow-1343 --split dev --seed 0 \
  --provider bigmodel --model glm-5.3-flash-main \
  --budget 10 --repeats 1 --live-llm --record-llm \
  --artifact-root <新站点>/artifacts \
  --snapshot-root /root/cp-run/CodePilot-Repair/cache/swebench \
  --spec-snapshot <新站点>/env-specs-4.1.0.json \
  --harness-python /root/swebench-venv/bin/python \
  --run-label glm15-<日期>
```

硬预算设置：单题 ¥10（`--budget 10` 与派发层 `CODEPILOT_DISPATCH_BUDGET_CNY=10` 两道独立
强制，均在请求发出之前生效），`max_test_runs` 保持 12、`max_tool_calls_per_iteration` 保持
30、`max_review_calls` 保持 2 —— **不为触发该形状而放宽任何上限**。

判定标准（逐条核对新站点产物，不改写 glm12～glm14）：

1. `state_final.json.coding_stop_reason == "budget_tool_calls"` 且 `len(tool_calls) == 30`
   → 目标形状才算被真实观察到；
2. 该情形下 `closure.coding_stop_reason == "budget_tool_calls"` 而 `stop_reason` 另有其人
   （或为 `None` 的成功收尾）—— 局部原因绝不覆盖全局原因；
3. `closure.review_executed` 为 `true` **或** `review_skip_reason` 写明具体未执行原因，
   二者必有其一，不允许"没审却没说为什么"；
4. `closure_admission.feasible == true`，且 `coder_share + required ≤ remaining`；若真实
   运行里准入拒绝了某轮，必须能在 `events.jsonl` 读到 `refused` 行与同一句 `detail`；
5. 官方 verdict 只来自官方 Harness 报告，内部级别只来自 `verification_level(state)`；
6. 费用与调用数以派发账本为真值核对（见 §6 的对账口径）。

## 6. DA-023：修复后的记账验证与历史差异

`llm/tracking.py:tracks_usage()` 的查重（按**账本对象**沿 `inner` 逐层，带 `seen` 防环）
保持原实现不动，本轮只补验证：

- 保留并继续依赖 Phase 11 的图级用例
  `tests/graph/test_phase11_closure.py::test_a_reviewer_behind_a_decorator_is_booked_exactly_once`
  及其负控制（把判据退回 `isinstance` 版本会立刻失败在 `+2`）。
- `tests/unit/llm/test_tracking_chain_accounting.py` 扩展：深度 0～3 的装饰层下
  **一次请求 = 一次调用记录 + 一笔费用 + 一个 `by_role` 桶**（原来只数调用次数，金额翻倍
  看不出来）；交错装饰层（统计层夹在多层 `_Override` 中间）仍认同一本账；换一本
  `Usage`（另一个 run）时必须重新补层，两本账各记各的；防环用例保持。
- 新增 `tests/unit/llm/test_dispatch_ledger_reconciliation.py`：把三套口径的适用范围写成
  可执行断言 —— 派发账本行（每次真实 HTTP 尝试一行，先落盘再发请求）、内部
  `Usage.llm_calls`（每次经过统计层的 provider 调用）、provider `attempts`（含重试）。
  正例：N 次请求 ⇒ N 行 settled、N 次调用、同一笔费用、`outstanding()==0`。
  负控制：无条件再补一层统计层会立刻复现"1 行 vs 2 次记账"，差额恰等于一次审查费用
  （这就是 glm14 的可检测签名）。合法差异侧：结果不确定的请求留下 `unknown_frozen` 行与
  一次 `cost_known=False` 的 0 费用调用，两侧口径不同、不可直接相减，因此批跑自检必须
  按状态分桶比对而不是只比总数。

**glm14 的历史证据未被改写**：该次运行发生在修复之前，内部账本 26 次 / ¥0.19581048、
派发账本 25 行 / ¥0.19192368（25 行全部 settled、`frozen=false`），差额 ¥0.0038868 恰为
一次审查费用；`by_role` 里同时留着 `reviewer` 与 `review` 两个桶。这组数字按原样保留并在
台账中标注为**修复前的历史缺陷**，事后核对以派发账本为真值；本轮新增用例验证的是**修复后
的实现**，不把旧产物伪装成修复后的结果。

## 7. 统一门禁

环境：WSL2 Ubuntu-24.04（原生 Linux git、Python 3.12.3），代码由宿主工作树 rsync 进
`/root/cp-run/CodePilot-Repair` 的既有 `.venv`；同一份最终树（`SYNCED_SOURCE_SHA256=682f048fe3953285`，
HEAD `55a4380` + 本轮未提交改动）串行执行。日志与 junit 留在
`/root/cp-run/p12/stageA-{static,offline,docker-probe}.log`、`/root/cp-run/p12/stageA-offline.xml`。

| # | 门禁 | 命令（均在 `.venv` 内） | 结果 |
| --- | --- | --- | --- |
| 1 | 静态检查 | `ruff check .` | All checks passed，exit **0** |
| 2 | 格式 | `ruff format --check .` | 388 files already formatted，exit **0** |
| 3 | 类型 | `mypy src` | Success: no issues found in 170 source files，exit **0** |
| 4 | 全量离线（CI 同参） | `pytest -m "not docker and not llm" --cov --cov-report=term` | **3448 passed / 6 skipped / 50 deselected**，exit **0**，739.77 s，覆盖率 **91%**（`Total coverage: 90.81%`，80.0% 的阈值未下调） |
| 5 | 真实 Docker | `pytest -s -m docker --no-cov` | **50 passed / 0 failed / 3454 deselected**，exit **0**，330.34 s；结束后 `codepilot` 标签容器 **0** 个、卷 **0** 个（Phase 11 是 48 个，本轮新增的 2 个真实容器用例已计入）。日志 `/root/cp-run/p12/stageB-docker.log`、junit `stageB-docker.xml` |
| 6 | 779 条基线生产链路复跑 | `.venv/bin/python scripts/verify_baseline_nodeids.py --site /root/cp-run/phase12-nodeids-20261021 --mirror /root/cp-run/phase11-glm12-20261020/artifacts/repo-cache/8b675b79e9201815 --archived-state …/state_final.json --patch …/patch_final.diff --expected-image-id sha256:a61197e0b294…` | **25 条判据全 PASS**（缺失 0 / 重复 0 / `new_failures=0` / L3 回读 779 条与授权清单同一哈希 / 13 条含空格引号的参数化 ID 放行 / 14 条注入负控制全拒 / 容器与卷零新增残留 / 模型调用 **0** 次），exit **0**，22.37 s。新站点、未覆盖任何历史产物。日志 `/root/cp-run/p12/stageB-nodeids.log`，产物 `/root/cp-run/phase12-nodeids-20261021/` |

**门禁 5/6 的执行经过（环境事件，如实记录）**：这两项第一次尝试时**跑不起来**——Docker Desktop
的 WSL 集成在 `Ubuntu-24.04` 被发行版空闲停用并重启后没有重新注入
`guest-services/docker.proxy.sock`（目录为空），Python SDK 直接
`DockerException: FileNotFoundError`，而 Windows 侧引擎本身健康。按顺序做了两次**经用户授权**的处置：

1. `wsl --terminate Ubuntu-24.04` 再启动（最轻、不动引擎）→ socket **仍未回来**。原因：WSL 的 VM
   因为 `docker-desktop` 发行版一直在跑而没有下线，而 socket 注入是按 VM 会话触发的。
2. `docker desktop restart`（用户明确授权，知道会短暂影响其它容器）→ 引擎恢复（Server 29.8.2），
   `guest-services/*.sock` 重新出现，WSL 侧 `docker info` 通。重启前已快照 19 个在跑的容器与各自的
   `HostConfig.RestartPolicy.Name`；重启后 14 个 `unless-stopped` 自行带回，4 个
   `policy=no` 的 `traceinquest-w17-pg / -w17-linux / -w17-node / -w18-linux` 由我逐个 `docker start`
   带回原状态。**最终运行集与重启前逐字相同（差集为空）**，除这 4 个"恢复原状"的启动之外
   没有对用户的容器做任何操作。

门禁 5/6 在恢复之后**在同一份最终工作树上**串行实跑，数字见上表；期间还遇到一次
`Wsl/Service/WSAETIMEDOUT`（发行版刚被 Desktop 带停、启动超时），用重试包装器重发即可，
不影响结果。

`pytest -m docker` 的用例数从 Phase 11 的 48 增至 **50**，正是本轮新增的两个真实容器准入用例；
**没有新增 skip、没有降低阈值、没有弱化断言**（离线集合的 6 个 skip 仍是既有的 Windows 专属用例
与 `CODEPILOT_TEST_EMBEDDING_MODEL` 未设置）。

6 个 skip 全部是既有的 Windows 专属用例（`taskkill` 路径、junction 重解析点 ×4）与
`CODEPILOT_TEST_EMBEDDING_MODEL` 未设置；**没有新增 skip、没有降低阈值、没有弱化断言**。

## 8. 剩余风险与未执行项

- **门禁 1–6 已在同一份最终工作树上全部实跑通过**（数字与证据路径见 §7）。过程中的一次
  Docker 通道中断与恢复经过也记在 §7，没有以跳过、改断言或降阈值换取绿灯。因此
  **本轮可宣称工程验收完成**；但"工程验收完成"只覆盖到状态机、预算机制、离线与真实容器
  验证这一层，**不**等于真实模型效果已被验证（见下条）。
- 本机 WSL2 门禁数字**不等于** GitHub Actions 通过（分支未 push，CI **NOT RUN**，DA-003 原状态）。
- **真实模型同形状（DA-022）**：`REAL SAME-SHAPE NOT OBSERVED` 维持。工程侧证据已齐，
  缺的只是一次授权后的真实单题复验（命令与判定标准见 §5）。
- **批量与费用校准**：test50 / test300、10 题费用校准、GitHub Actions CI 全部 **NOT RUN**
  （DA-017 / DA-019 / DA-003 状态不变）。本轮数字来自 WSL2 本机门禁，不得写成 CI 已通过。
- **准入判定只看"次数"这一维**：回归耗时属于墙钟与 `sampled` 判定，不在准入里。一次
  轮数足够但墙钟不足的 run 仍会以既有方式记录 `regression_incomplete`，不会伪装成通过。
- **份额机制对"模型坚持自跑测试"的实际效果需真实复验**：离线证明的是工具层拒绝与计数
  不变量；真实模型在被拒绝后是否改走别的工具组合（从而更快触到 30 次工具上限）尚未实测，
  归入 §5 那次授权复验的观察项。
- 结论口径不变：官方 `RESOLVED` 只由官方 Harness 决定，内部 `m2_verified` 只由既有 M2
  完整验证 + 审查绑定规则决定，两者互不覆盖；未执行阶段一律如实写 NOT EXECUTED。
