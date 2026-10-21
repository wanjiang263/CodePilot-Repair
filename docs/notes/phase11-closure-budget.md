# Phase 11：M2 自验证闭环修复与预算机制加固

本轮日期 2026-10-20。分支 `codex/phase11-runtime-acceptance`，基线 HEAD `55a4380`
（含此前尚未提交的 DockerSandbox 传输层修复，见
[phase11-sandbox-transfer.md](phase11-sandbox-transfer.md)）。

本轮**只做工程开发与离线验证**：0 次真实 GLM 或其他付费模型调用、0 个新的
SWE-bench 真实评测、未运行 3～5 题批量。所有验证走 FakeLLM、确定性测试与既有真实
Docker 测试。

---

## 1. 缺陷与根因

首次真实官方判分成功（`marshmallow-code__marshmallow-1343` → 官方 **RESOLVED**，
45 次调用 ¥0.2817）暴露的是一条工作流缺陷，而不是模型能力缺陷：

```text
repair_patch 的编码循环撞到每轮 30 次工具调用上限
  → _run_coder 把 run 写成 status=BUDGET_EXHAUSTED
  → routing.terminated() 对任何非 running 状态一律短路到 finalize
  → PatchValidator / 目标测试 / 回归 / PatchReviewer 全部被跳过
  → 报告里 agent_claims_success=false、verification_mode=none
```

根因是把**两种不同作用域的预算写进了一层**：`max_tool_calls_per_iteration` 是
"这一轮还能不能继续改代码"，而 `status` 是"这次任务结不结束"。局部额度耗尽被当成
全局终止，于是 Agent 无法完成自身的验证闭环——补丁是对的，但系统不知道自己为什么
认为它是对的。

---

## 2. 四种预算的实际生效位置（本轮核对结果）

| 层 | 上限来源 | 计数口径 | 拦住谁 | 本轮变化 |
| --- | --- | --- | --- | --- |
| 工具调用（局部） | `RepairBudget.max_tool_calls_per_iteration`（默认与 `configs/full.yaml` 均为 30；`CodingAgent` 构造函数拒绝 >30 的硬上限） | `len(state.tool_calls)`，**只由编码节点整体覆盖** | 当前这一轮编码循环 | 作用域收窄：不再决定图是否进 finalize |
| 修复迭代 | `max_iterations=5`（首轮 `generate_patch` 不计入） | `len(attempts) - 1` | `repair_patch` 派发 | 不变 |
| 模型调用与费用 | `max_total_tokens=1_000_000`、`max_cost`（用户设定；两次真实运行用 ¥10） | `state.usage`（累计绝对值） | 每个节点入口 + 编码循环内每一圈 | **新增闭环预留**：编码派发前扣除验证/审查额度 |
| 验证与审查 | `max_test_runs=12`（含基线 2 次）、`TestRunBudget` 在工具层同源、**新增** `max_review_calls=2` | `state.test_runs`（`AccountingSandbox`）、`state.review_calls` | 测试节点入口、`review` 节点 | 审查额度独立计数 |

外层还有两道不属于 M2 图、但真实运行时更硬的闸门，本轮**未放松**：

* `llm/dispatch_budget.py`：逐 HTTP 尝试按**未缓存全价**预留整个上下文窗口，凭据
  缺失、用量越界或费用不确定即冻结派发（fail closed）；账本文件先落盘再发请求。
* `builder.recursion_limit_for` = `20 + 6 × max_iterations`，只作为路由写错时的最后
  一道防线，不是停止机制。

工具权限、PatchValidator 的 `max_files_changed=5` / `max_lines_changed=150`、测试文件
授权与 Gold 隔离规则全部保持原状；闭环便利没有被用来降低任何安全约束。

### 预留的算法

`BudgetManager.closure_reservation(state, reviewer_available=…, re_review_possible=…,
reviewer_config=…)`：

* 每次审查按 `REVIEW_CONTEXT_TOKENS(24_000) + REVIEW_OUTPUT_TOKENS(4_096)` 计，
  常数与 `agents/reviewer.py` 用的是同一份（改一处不会漂移）；观测到的"平均每调用
  token"更大时取更大者 —— 预留只能取较坏情况。
* 次数：有 Reviewer 时初审 1 次，Phase 9 高危修复政策允许再复审时 2 次；没有
  Reviewer 时预留 0（审查会如实记为未执行，不为它空占额度）。
* 费用：**优先**按 Reviewer 实际绑定模型的单价估算。`reviewer_config` 就是生效的
  `LLMConfig`（装配层由 `ProviderBundle.registry.config` 传进 `Deps.llm_config`），
  `resolve_role("reviewer")` → `ModelPrice` 的输入价与输出价 →
  `llm.cost.compute_cost` 换算到 `budget_currency`。缓存价**刻意不折价**：审查上下文
  命中缓存是最好情况，预留要担保的是最坏情况；观测均值大于审查常数时，超出的 token
  按输入价与输出价中**较贵**的那一档计。
* 口径写在 `ClosureReservation.basis` 里：`reviewer-model:<别名>` / `observed-unit` /
  `unknown` / `no-reviewer`，并随预算检查写进 `events.jsonl` 与阻断消息。
* 单价缺失或不完备、或币种与预算不一致**且缺汇率**时，退回账本实测单价
  `state.usage.cost / state.usage.total_tokens`；账本也不可用（有未知费用调用或一次
  调用都没有）时 `estimated=false`。缺汇率按 1.0 换算会把 USD/CNY 的 7 倍差额算成
  "价格差不多"，那种数字比"估不出来"更危险，所以当作估不出来。

预留**不是记账**：从不写入 `state.usage`，因此不可能重复计费；它只做一件事 —— 把
消耗型活动（`CODEGEN` / `REPAIR`）的可用上限压低。作用在两个点上：

1. **派发前判定**：剩余不足 → 直接阻断编码派发（测试见
   `test_closure_reservation_blocks_the_coding_dispatch_before_any_spend` 与
   `test_the_closure_reservation_is_priced_by_the_reviewers_own_model`）。
2. **循环内判定**：净去预留后的上限作为 `token_ceiling` / `cost_ceiling` 传给
   `CodingAgent.run()`，在每一圈循环里判定。只在节点入口检查等于把闭环额度交给
   编码阶段随意支配 —— 一次编码可能有几十次模型调用。

为什么平均单价不够（本轮第二处修复）：`已花费用 / 已花 token` 的口径是"已经跑过的那些
调用"，而 Coder 与 Reviewer 常是不同模型。编码便宜、审查贵时，按平均单价估出来的预留
**系统性偏低**，闭环照样饿死 —— 只是从"被工具额度跳过"变成"被钱卡住"。图级用例把上限
钉在两种口径的中间（账本口径付得起、审查单价口径付不起），因此这一条不是概念上的修正
而是可证的差异。

没有为测试次数与迭代数设预留：`max_iterations` 已由 `check_budget` 在每次修复派发前
判定，测试次数在每个测试节点入口判定。那是既有的安全降级 —— 补丁会留在"未验证"
状态并被如实报告，而不是被伪造成通过。不可回收的只有 token 与费用，所以只有它们
必须在派发前留出来。

---

## 3. 状态转移（修复后的受限状态机）

正常路径：编码 → 补丁校验 → 目标测试 → 回归 → 独立审查 → finalize。
存在失败时：失败证据 → Failure Diagnosis → 有界修复 → 重新校验 → 重新测试 → 重新审查。

| 场景 | 判据 | 转移 | 终态与报告 |
| --- | --- | --- | --- |
| A 额度耗尽 + 有效补丁 | `patch` 非空 | **不写 status**；冻结 `frozen_patch_hash`，记 `coding_stop_reason`，继续 validate → tests → regression → review | 可得到 `succeeded` / `m2_verified`；`stop_reason` 为 `None`，局部原因单独成列 |
| B 额度耗尽 + 无补丁 | `patch` 为空 | 真终态 → finalize | `budget_exhausted` + `budget_tool_calls`，失败原因写 `EMPTY_PATCH`，审查记 NOT EXECUTED |
| B′ 额度耗尽 + 补丁无效 | validator 拒绝 | validate → check_budget（工具额度即在此拦停）→ finalize | 不进成功收尾，不执行测试、不调用审查 |
| C 测试失败 | `execution != PASS` | diagnose → check_budget（此时局部额度拦下"再来一轮"）→ finalize | 有诊断证据，无成功声称 |
| D 审查 high | `review.has_high_issues` | 一次额外修复（`review_repairs<=1` 由 Schema 钉死）→ 重新校验/测试/回归/审查；第二次 high → finalize | `stopped` + "unresolved high review issues"，问题保留在报告里 |
| E 硬预算/基础设施失败 | `status` 已是终态 | 任何一条边都短路到 finalize | 已有测试证据保留，未执行的审查/回归**不得**标为通过 |

`review` 节点新增三条规矩：前置条件不满足就**不调用模型**（原因写进
`review_skip_reason`）；结论盖上被审补丁的 SHA-256（`reviewed_patch_hash`）；同版本
重放复用已有结论，不重复调用、不重复计数。`finalize` 只接受"审查结论绑定当前候选
补丁"的成功，并把未执行阶段逐条写成 NOT EXECUTED。

`review_preconditions()` 是这条判据的唯一实现，`routing` 与 `PatchReviewer` 共用，
避免出现"图以为能审、Reviewer 自己拒审"的两套口径。

---

## 4. 报告与终态语义

`RunReport.closure`（`VerificationClosure`）逐阶段回答任务书 §六 的每一问：补丁与
SHA-256、编码局部停止原因、validator 是否执行及结果、目标测试状态、L1/L2/L3 与
新增失败数、Reviewer 是否执行/结论/未解决 high、是否发生审查后修复、审查专属额度
用量、七类预算读数、硬费用上限是否触顶、未执行阶段列表。`verification_level` 抽成
`report.verification_level(state)` 单函数，评测装配层与报告同源。

`InferenceOutcome` 增加七个可选字段（`verification_level` / `review_executed` /
`coding_stop_reason` / `review_high_issues` / `post_review_repair` /
`unexecuted_stages` / `review_bound_to_candidate`），与报告里的 closure 同源
（`VerificationClosure.from_state(state)` 只算一次，两处各判一遍迟早会出现"报告说没审、
结果行说审过了"）。这些字段继续写进 `TaskRecord`，于是 `results.jsonl` 逐行就能区分
"官方没判"、"内部没审"、"审了但不是这一版补丁"，而官方判分仍然只来自官方报告：
`compare_with_internal_m2` 与 `INTERNAL_NOT_CLAIM_OFFICIAL_RESOLVED` 机制原样保留，
两者互不覆盖。SWE-bench 的恢复路径从 run 现场 manifest 读回同一组字段，避免把
"已经审过"的现场写成"从未执行"。

结论口径不变：官方 `RESOLVED` 只由官方 Harness 决定；`m2_verified` 只在既有 M2 完整
验证 + 审查规则满足时成立；未执行 Reviewer 记为未执行。历史实验结果与 Artifact
未被改写。

---

## 5. 测试与门禁

新增/改写的回归测试（全部 FakeLLM + 确定性工具，真实 git 夹具与真实
PatchValidator/RegressionRunner）：

| 用例 | 覆盖任务书条目 |
| --- | --- |
| `tests/graph/test_phase11_closure.py::test_coder_tool_cap_with_valid_patch_still_runs_the_whole_closure` | §四 场景 A、§七.1，以及要求的**完整图级集成测试**（30 次工具上限 + 有效补丁 → validator/真实测试证据校验/Reviewer/正确报告） |
| `…::test_tool_cap_without_any_patch_terminates_without_a_success_claim` | 场景 B、§七.2 |
| `…::test_tool_cap_with_a_patch_the_validator_rejects_does_not_finish_successful` | 场景 B′、§七.3 |
| `…::test_high_review_gets_exactly_one_extra_repair_then_stops_safely` | 场景 D、§七.4/5 |
| `…::test_low_or_medium_review_closes_without_an_extra_repair` | 场景 A/D、§七.6 |
| `…::test_target_test_failure_goes_to_diagnosis_not_a_success_claim` | 场景 C、§七.7 |
| `…::test_a_review_of_an_older_patch_never_blesses_the_new_candidate` | §五.1/5、§七.8、重放不重复计数 |
| `…::test_a_review_blocked_by_its_own_budget_records_the_stage_as_unexecuted` | §二.2 预算不足：审查被 `budget_review_calls` 拦下时不许向模型要结论，且 `review_skip_reason` 必须写明是额度；同时证明"同版补丁重放优先于额度检查" |
| `…::test_local_tool_exhaustion_does_not_consume_the_review_allowance` | §三.3、§七.9 |
| `…::test_closure_reservation_blocks_the_coding_dispatch_before_any_spend` | §三.4/5、§七.10（含"腾出预留即可正常派发"的负控制） |
| `…::test_the_closure_reservation_is_priced_by_the_reviewers_own_model` | §三.1：**编码便宜、审查贵**时按审查模型单价必须阻断派发，按账本平均单价不会；含"审查与编码同价则照常派发"的负控制 |
| `…::test_reservation_falls_back_to_the_ledger_and_says_so` | §三.1 回退口径：无可采信单价时退回账本单价，并把 `basis=observed-unit` 写进阻断消息 |
| `…::test_missing_regression_records_review_as_not_executed_instead_of_calling_it` | 场景 B/E、§七.12 |
| `…::test_official_resolved_and_internal_open_loop_are_kept_apart` | §六、§七.13 |
| `…::test_a_patch_in_hand_keeps_the_graph_on_the_verification_path` / `test_no_patch_in_hand_short_circuits_the_graph_to_finalize` | §七.14（图级状态转移） |
| `…::test_a_crashed_coding_stage_leaves_no_stale_reservation` | §七.11（异常后重跑：天花板不变、账本只含真实调用、无幻影费用） |
| `tests/unit/graph/test_closure_budget.py`（34 项） | 预留算法、Reviewer 单价优先（不同模型/不同输入输出价/缓存价不折价/超出部分按较贵单价）、币种与汇率与未知单价的 fail closed、FakeLLM 与免费额度零成本负控制、只收紧不放松硬上限、预留非记账、跨进程恢复逐字节同判、审查专属额度 |
| `tests/unit/llm/test_dispatch_budget.py` | 派发层逐请求预留：用量越界/费用不确定即冻结账本；缺价、单价不全、币种不是预算币种时**一次请求都不发** |
| `tests/unit/tools/test_write_tools_registry.py::test_parametrized_node_ids_with_spaces_are_targets_not_commands` / `::test_command_shaped_targets_are_still_rejected` | 真实第三次运行暴露的缺陷：含空格/引号的参数化 NodeID 必须是合法目标；命令形态（flag 开头、`;`、`$( )`、换行、超长、越界）仍然一律拒 |
| `tests/integration/test_swebench_smoke.py::test_cost_note_follows_the_engine_and_never_calls_real_money_a_demo` | 真实运行的 cost.json 不许写"这只是记账链路接通"；模拟运行也不许写成真实花费 |
| `tests/unit/graph/test_routing.py`（新增 8 项 + 9 条参数化前置条件表） | 场景 A–E 的纯路由判据 |
| `tests/unit/agents/test_coder.py`（新增 3 项） | 局部 token/费用上限、只能收紧不能放宽 |
| `tests/graph/test_path_budget.py`（改写工具上限用例） | 局部额度不再跳过验证，同时仍是权威 StopReason |
| `tests/graph/test_path_first_pass.py`（加强边界断言） | 未执行阶段必须逐条写 NOT EXECUTED |

离线门禁本轮实跑结果见 §6。真实模型批量、真实 GLM 单题复验 **NOT RUN**（本轮授权
范围之外）。

---

## 6. 验收记录

### 6.0 本轮收口（2026-10-20 第二次实跑，最终树）

环境：WSL2 Ubuntu-24.04，代码由宿主工作树 rsync 进 `/root/cp-run/CodePilot-Repair`
（已有 `.venv`），门禁脚本 `/root/cp-run/final-round-gates.sh`（三段各自留日志与退出码）。

| 门禁 | 命令（均在 `.venv` 内） | 结果 |
| --- | --- | --- |
| 静态检查 | `ruff check .` | All checks passed，exit **0** |
| 格式 | `ruff format --check .` | 382 files already formatted，exit **0** |
| 类型 | `mypy src` | Success: no issues found in 170 source files，exit **0** |
| 定向测试 | `pytest tests/graph tests/unit/graph tests/unit/llm tests/eval -q`（含真实 Docker 图级用例） | **1301 passed**，exit 0，814.50s（同一轮修复前的中间树） |
| 全量离线门禁（CI 同参） | `pytest -m "not docker and not llm" --cov --cov-report=term` | **3367 passed / 6 skipped / 46 deselected**，exit **0**，481.39s，覆盖率 **91%**（阈值 80%） |
| 真实 Docker | `pytest -s -m docker --no-cov` | **46 passed / 0 failed / 3373 deselected**，exit **0**，632.59s |

真实 Docker 一项**本轮转绿**：上一轮记 ENVIRONMENT_BLOCKED 的
`tests/integration/test_docker_phase1.py::test_phase1_contracts_in_linux_container`
（`docker build --progress=plain`）现在在本地真实 daemon 上执行并通过，
容器与镜像均无残留（`docker ps -a` 里 `codepilot*` 0 个、`codepilot-phase1-test:*`
镜像 0 个）。ENVIRONMENT_BLOCKED 一项已消除，不再以跳过或改断言换取绿灯。

本轮新增/改写的用例集中在预留单价口径、审查节点判定顺序、真实基线 NodeID 判据与
`results.jsonl` 闭环字段：`tests/unit/graph/test_closure_budget.py` 34 项、
`tests/unit/llm/test_dispatch_budget.py` 9 项、`tests/graph/test_phase11_closure.py` 17 项、
`tests/unit/tools/test_write_tools_registry.py` 5 项（新增 2）、
`tests/eval/test_runner.py` 新增闭环独立性与 journal 往返用例、
`tests/integration/test_swebench_smoke.py` 新增 cost note 按引擎分叉用例。
6 个 skip 仍是既有的 Windows 专属用例与
`CODEPILOT_TEST_EMBEDDING_MODEL` 未设置；**没有新增 skip、没有降低阈值、没有弱化断言**。

Docker 的 `--progress` 阻断是**分三层**剥开的，最终真正解决：

1. **客户端能力**：本机 `/usr/local/bin/docker` 是 27.5.1 legacy-builder 静态客户端，
   没有 buildx 插件 → `unknown flag: --progress`。改用 **Ubuntu 官方源**的
   `docker-buildx` 0.30.1（签名发行包，不下载第三方二进制）后 BuildKit 真的跑起来了
   （日志 `#0 building with "default instance" using docker driver`）。
2. **装包带来的副作用**：`docker-buildx` 硬依赖 docker-cli，apt 通过 Recommends 连带装了
   `docker.io` + `ubuntu-fan`，并在 WSL 里起了**第二个 dockerd**抢占 `/var/run/docker.sock`，
   使 Docker Desktop 的 WSL 通道失联（`shared-sockets/host-services/backend.sock` 不再重建）。
   处置：`systemctl stop/disable docker.service docker.socket` + 删除残留 sock 文件；
   第一次 `apt remove docker.io ubuntu-fan` 连带把 buildx 也卸了（它依赖 docker-cli），
   最终安装顺序固定为
   `apt-get install -y --no-install-recommends docker-buildx` **然后立即**
   `systemctl disable --now docker.service docker.socket`，只留 CLI 插件、不起守护进程。
3. **宿主端恢复**：Desktop 的 WSL 集成需要宿主端重新挂载。经用户明确授权后执行
   `docker desktop restart`（先记录当时 18 个运行中容器，重启后把 `unless-stopped`
   没自动带回的 4 个逐个 `docker start`，最终 18/18 回到重启前状态）。
   紧接着 registry 恢复可达，拉下了该用例需要的
   `python:3.12.7-slim-bookworm` 与 `ghcr.io/astral-sh/uv:0.12.9` 两个基础镜像。

期间一度出现"外部 registry 不可达"（`auth.docker.io … connection refused` /
`registry-1.docker.io … i/o timeout`），那是 Desktop 通道失联期间的表象，恢复后消失。
最终该用例通过，本轮**没有任何 ENVIRONMENT_BLOCKED 遗留**。

### 6.1 上一轮（同分支，工程收口轮）实跑存档

环境：WSL2 Ubuntu-24.04（原生 Linux git 2.43、Python 3.12.3、项目 `.venv`），Docker 经
Docker Desktop 的 guest socket（`DOCKER_HOST=unix:///mnt/wsl/docker-desktop/shared-sockets/guest-services/docker.proxy.sock`，
Engine API 1.56）。代码与测试由宿主工作树 rsync 进 ext4 副本 `/root/cp-run/CodePilot-Repair`
后执行；Windows 侧的 Git-for-Windows `sh.exe` 命名对象限制依旧存在（克隆类用例在宿主
Python 沙箱里必然失败），因此**门禁一律以 Linux 实跑为准**。

| 门禁 | 命令 | 结果 |
| --- | --- | --- |
| 静态检查 | `ruff check .` | All checks passed（exit 0） |
| 格式 | `ruff format --check .` | 382 files already formatted（exit 0） |
| 类型 | `mypy src` | Success: no issues found in 170 source files |
| 定向测试 | `pytest tests/graph tests/unit/graph tests/unit/agents tests/integration -m <none>` | **775 passed / 1 failed**，唯一失败为下表的 Docker CLI 用例（环境阻断，非回归） |
| 全量离线门禁（CI 同参） | `pytest -m "not docker and not llm" --cov` | **3344 passed / 6 skipped / 46 deselected，exit 0，510.95s，覆盖率 90.70%**（门禁阈值 80%） |
| 真实 Docker | `pytest -s -m docker` | **45 passed / 1 failed / 3350 deselected，261.03s** |

6 个 skip 全部是既有的 Windows 专属用例（taskkill 路径、junction 重解析点 ×4）与
`CODEPILOT_TEST_EMBEDDING_MODEL` 未设置；本轮**没有新增任何 skip、没有降低覆盖率阈值、
没有放宽断言**。

### 6.1 真实 Docker 覆盖到的部分

通过的是走 Python Docker SDK 的真实容器用例：`tests/docker/*`（含上一轮未提交的传输层
修复回归 `tests/docker/test_transfer.py`、网络/超时/OOM/用户/环境缓存）、
`tests/graph/test_phase9_docker.py`（真实镜像上的回归与审查 E2E）、
`tests/graph/test_minimal_e2e.py::test_fake_llm_with_real_docker_executes_controlled_fixture`、
`tests/integration/test_docker_phase1.py` 之外的容器契约用例。也就是说：闭环修复所依赖的
**真实容器执行路径**（目标测试、回归、证据落盘）在本机真实 daemon 上跑通了。

唯一失败：`tests/integration/test_docker_phase1.py::test_phase1_contracts_in_linux_container`
——它 shell 调用 `docker build --progress=plain`，而本机 WSL 里装的是静态
`docker` 27.5.1 legacy-builder 客户端（无 buildx），直接报
`unknown flag: --progress`。判据与本轮改动无关：在仓库外用一个只有
`FROM hello-world` 的裸 Dockerfile 执行同一条命令，得到同样的报错。CI 的 ubuntu runner
带 BuildKit，该用例在 CI 上可执行。**状态记为 ENVIRONMENT_BLOCKED，不记 PASS**。

### 6.2 未执行 / 不在本轮范围

* 真实 GLM 或其他付费模型调用：**0 次**（本轮授权范围之外）。
* 新的 SWE-bench 真实评测：**0 个**；3～5 题批量：**未运行**。
* 首次真实运行那条"预算停在编码阶段"的路径未在真实模型 + 真实失败日志上复验 → DA-022。
* GitHub Actions CI：分支未 push，**NOT RUN**（DA-003 维持原状态）。

> §6.1–§6.2 是**上一轮**（同分支工程收口轮）的存档，其中"真实调用 0 次"与
> "`docker build --progress` 记 ENVIRONMENT_BLOCKED"都对那一轮成立。本轮的最终数字见
> §6.0，真实单题验收见 §7。

## 7. 真实单题端到端验收（本轮执行，两次运行）

任务书要求工程收口与真实验收在同一轮完成，因此本轮**授权后**跑了真实模型：
同一实例 `marshmallow-code__marshmallow-1343`（dev / seed 0）、模型
`glm-5.3-flash-main`（BigModel，全部角色同别名）、官方 `swebench==4.1.0`、
单题硬上限 ¥10（`--budget 10` 与派发层 `CODEPILOT_DISPATCH_BUDGET_CNY=10` 两道独立
强制，均在请求发出之前生效）。两次都是新站点、新 run_id、新账本，只跑这一题，
没有触碰 test50/test300，也没有覆盖任何历史产物。

| 核对项 | attempt-1（glm12） | attempt-2（glm13） |
| --- | --- | --- |
| 官方 verdict | **RESOLVED**（`resolved_instances:1`/`error_instances:0`） | **UNRESOLVED**（`resolved_instances:0`/`error_instances:0`，decision `EXECUTED`） |
| smoke 状态 | `SMOKE_COMPLETED_RESOLVED` / attribution `official_resolved` | `SMOKE_COMPLETED_UNRESOLVED` / `official_not_resolved` |
| 补丁 | 550 B `a99f52042800c095…`（prediction 内嵌哈希一致，非 Gold） | 576 B `5c41d93b087c0446…` |
| 编码停止原因 | `coding_stop_reason=None`（5 次工具调用后 `finish`，上限 30） | 同左 |
| 校验 / 目标测试 | passed / **PASS**（身份 `de24a34b…` 与冻结值一致） | passed / **FAIL**（两轮 Attempt 补丁哈希相同 → `repeated_patch`） |
| 回归 | **未执行**（harness 缺陷，见下） | 未执行（前置条件不成立，如实记录） |
| 审查 | **未执行**，`review_calls=0` | 未执行，`review_calls=0` |
| 内部终态 / 级别 | `error` + `workflow_error`，`error_class=harness_internal_error`、`origin=harness` | `stopped` + `repeated_patch` |
| 调用 / token / 费用 | 13 次；178,985 in / 4,224 out / 55,680 cached；**¥0.1232776** | 19 次；266,698 in / 6,888 out / 108,416 cached；**¥0.1708477** |
| 账本 | `limit=10.0`、`frozen=false`、13 条全 settled | `limit=10.0`、`frozen=false`、19 条全 settled |
| 容器清理 | `codepilot*`/`swebench*` 残留 0；实例镜像按批准保留 | 同左 |

两份完整记录：`artifacts/phase11-glm12-20261020/REPORT.md`、
`artifacts/phase11-glm13-20261020/REPORT.md`。

### 7.1 真实运行抓到的缺陷（attempt-1）

`run_regression` 逐条校验基线 NodeID 时，把真实 marshmallow 基线里 9 条
**含空格/引号的 pytest 参数化 ID** 判成命令行，抛
`ValueError: test target must be a path or pytest NodeID, never a command`，
回归整段中止、审查从未执行。FakeLLM 夹具的 ID 全是 `test_ok` 这种最规整形式，
所以这条在离线全绿的情况下潜伏至今。修法是分段判定：路径部分严格，`::` 之后的
ID 本体放行；注入面由沙箱层逐项 `shlex.quote` + `_validate_test_selection()`
（flag / NUL / 换行 / 越界）承担，两项均未放松。修复证据除新增单测外，还有一次
**零模型成本**的真实容器实测：在实例镜像内直接跑两条带空格 ID → `2 passed`，
且 JUnit 回读经 `sandbox/junit._node_id()` 还原后与输入 ID 逐字相等。

### 7.2 真实验收证明了什么、没证明什么

- **证明**：推理工作区可在 WSL 侧无 GitHub 出口建立（base-only 浅镜像通道，
  `stored_commits=1` + `fsck` + `_assert_isolated` 双次通过）；完整 M2 图 → 补丁 →
  predictions → 官方 Harness 判分的链路在真实模型下端到端可跑；官方 `RESOLVED`
  与内部闭环结论**确实各记各的**（attempt-1 就是"官方判对、内部报错"的组合）；
  ¥10 硬上限、逐请求预留与容器清理有效。
- **没证明**：DA-022 的目标形状 —— 编码阶段撞到每轮 30 次工具上限后闭环仍走完
  验证与审查。两次真实运行模型都在 5 次工具调用后自行 `finish`，从未触顶，
  因此该条继续记 **NOT RUN**，不因"跑了真实题"而改判。
- 按本轮新口径，这一题的闭环预留应为 2 × (24,000×0.8 + 4,096×2.8)/1e6 =
  **¥0.0613376**，basis `reviewer-model:glm-5.3-flash-main`；两次运行都没有让它生效
  （远未触顶），所以这个估算值与实际审查花费的偏差量级仍是批量前必须复核的一项。

## 8. 残留风险

* **闭环修复只在 FakeLLM + 脚本化 Reviewer 上验证过**。真实模型是否会在冻结补丁上
  产出与脚本不同的失败形态（比如审查证据行号越界、回归抽样命中率不同），需要下一次
  **授权后**的真实单题复验 → 台账 DA-022。
* 预留的费用口径已经改为**优先按 Reviewer 绑定模型的单价**（§2）。仍然存在的下界缺口：
  预留假设审查上下文**全部不命中缓存**，所以它偏保守而不是偏小；反过来，若服务商按
  上下文长度阶梯计价（越长越贵），常数 24k 对应的单价可能与真实 30k+ 的档位不同。
  首次真实 smoke 还抓到一个方向相反的错误：最初把"0 token / 0 费用"也当成"估不出来"，
  导致 FakeLLM 运行在编码派发前被误阻断（`SMOKE_COMPLETED_NO_PATCH`），已改为"只有
  存在未知费用调用或一次调用都没有"才 fail closed，并补了
  `test_observed_zero_cost_is_a_real_reading_not_an_estimation_gap` 钉住这条边界。
* **预留只收紧 `CODEGEN`/`REPAIR`，不收紧 `LLM` 活动**（任务书 §三.1 的口径）。修复轮里
  `diagnose` 在编码之后、审查之前执行，用的是未预留的全局额度：极端情况下诊断可以把
  留给审查的 headroom 花掉。后果不是"悄悄跳过审查"，而是 `review` 节点入口的
  `Activity.REVIEW` 判定真的拦下它 → 终态 `budget_exhausted` + `budget_tokens`/`budget_cost`，
  `review_skip_reason` 写明"未执行"，报告把 review 列为 NOT EXECUTED。留作已知残余风险，
  没有为它放宽任何硬上限。
* `docs/02-实现方案.md` 已在本轮收口：§15.1 的 state 字段、§15.2.1 的闭环转移、
  §15.3.1/§15.3.2 的预算分工与闭环预留（含单价口径）、§15.5 的停止原因表与优先级、
  §15.8 的三份结论各自记录口径。
* `max_review_calls=2` 依据"初审 + 一次复审"的政策推导，属于**新增预算旋钮**；若未来
  审查轮次政策改变，必须同步改这里与 02 §15.3 的口径说明。默认值对既有配置是放宽的
  （原来审查只受 token/费用约束），任何想跑 3 次以上审查的用法都会被
  `budget_review_calls` 拦下并如实报告。
* Phase 5 的单次图（`graph/minimal.py`）仍把 `tool_limit` / `token_budget` 统一映射成
  `budget_tool_calls` 并直接终止 —— 那条路径上根本没有验证与审查节点，本轮刻意没有
  改动它，避免在闭环之外制造第二套语义。
* 测试次数与迭代次数没有进预留（只有 token/费用进）。预算不足时它们退回既有的
  逐节点安全降级：补丁留在未验证状态并如实报告未执行阶段，不会伪装成通过。

---

# 第三轮：最终收口（2026-10-20 第二次实跑，同一分支）

本轮做四件事：把预留真正盖到**所有排在审查之前的模型调用**；把"局部额度耗尽后的闭环"
在**真实容器**上验一遍；把真实基线 **779 条授权回归 NodeID** 用生产链路跑完整；然后
按顺序跑一次统一门禁与**最多一次**真实单题实验。

---

## 9. 预留覆盖范围（任务书 §三）

`RESERVATION_BEARING_ACTIVITIES` 从 `{CODEGEN, REPAIR}` 扩到
**`{LLM, CODEGEN, REPAIR}`**，落点在统一预算准入（`nodes._budget_guard` →
`BudgetManager.evaluate`），不是给每个节点另写一套算钱逻辑。

上一轮遗留的原话是："修复轮里 `diagnose` 在编码之后、审查之前执行，用的是未预留的
全局额度：极端情况下诊断可以把留给审查的 headroom 花掉"。这一条本轮被正式堵住：
`understand_issue` / `retrieve` / `localize` / `analyze_impact` / `plan` / `reproduce` /
`diagnose` 全部走 `Activity.LLM`，同样净去预留。`TEST` 与 `REVIEW` 仍然**不**吃预留——
它们是预留要保护的对象，把它们也收紧就是"为了保护闭环而把闭环跳过"。

三处配套口径：

1. **欠账计数**（`routing.pending_review_calls`）取代原来写死的 `re_review_possible`。
   预留只按"还欠几次审查"计：当前候选已审且无 high → 0；已审但带 high 且修复轮未用 → 1；
   没审过 → 1 + （修复轮还没用则再 +1）；专属额度花完或 run 已进终态 → 0，并且受剩余
   额度封顶。这样"审查已完成/不再可能"时预算立即释放，不会永久占用。
   **注意它与 `review_preconditions` 不是一回事**：后者判"此刻能不能审"，编码派发时补丁
   还不存在，用它算预留会永远得 0，"给审查留钱"这句话就从未成立过。
2. **预扣可见**（任务书 §三.4）。`state.usage` 只在响应回来之后累加，飞行中或已冻结的
   逐请求预留原本是隐形的。现在 `Deps.outstanding_cost`（装配层接到
   `dispatch_budget.current_outstanding`）把"已承诺未落账"这笔钱加进费用读数，**先加
   读数、再扣预留**，同一段钱不会被算两遍；未启用进程级账本时读数恒为 `None`，行为不变。
   币种换算不到预算币种时当作**未知**（`convert()` 缺汇率按 1.0 会把七倍差额读成"差不多"），
   由 `estimated=false` 的 fail-closed 分支拦下消耗型派发。
3. **模拟批次的零费用是合法读数**（本轮实跑抓出的回归）。预留扩到第一个 LLM 节点之后，
   "**账本还是空的**"发生在 `understand_issue` 上，而旧判据把"一次调用都还没有"当成
   "估不出来" → 每个设了 `max_cost` 的模拟批次在开局就被 `budget_cost` 拦住，
   实测形态 `SMOKE_COMPLETED_NO_PATCH`。收口口径改成按**费用来源**判断：
   `cost_unknown_calls > 0` 才是下界必须拦；`simulated_llm=true` 且账本为空是"已知的 0"
   （basis `simulated-zero-cost`）；真付费 provider 拿不到任何价格依据时照旧 fail closed。
   钉住这条边界的用例：`test_a_simulated_provider_with_an_empty_ledger_is_known_zero_not_unknown`。

测试证据（离线，全部 FakeLLM + 真实 git 夹具）：

| 用例 | 守住的任务书条目 |
| --- | --- |
| `tests/graph/test_phase11_closure.py::test_diagnose_cannot_spend_the_money_reserved_for_the_review` | §三.2/§三.3：Diagnose 被预留拦在**发请求之前**；含"连续两次判定同一原因、零新增支出"与"事件里带 basis"的证据要求，以及腾出预留后同一条边照常执行 |
| `…::test_the_reservation_is_released_once_no_review_is_pending` | §三.6：审查绑定当前候选后预留归零，紧上限不再拦住收尾前的最后一步 |
| `…::test_an_unsettled_precharge_is_visible_to_the_budget_gate` | §三.4：注入 0.6 预扣即拦住；预扣结算进账本后不会二次扣款 |
| `…::test_a_reviewer_behind_a_decorator_is_booked_exactly_once` | §三.5 + §十 的重复计费缺陷 |
| `tests/unit/graph/test_closure_budget.py`（现 47 项） | 收紧清单只有一份、审查与测试是受益方、欠账为 0 时不占用、已结算/预扣/预留三段各算一次、币种与缺汇率边界、负数预扣是配置错误、模拟零成本边界 |
| `tests/unit/graph/test_routing.py::test_pending_review_calls_follows_the_state_machine`（13 参数 + 1 项额度封顶） | 欠账计数的每一条状态机分支，含终态释放 |

## 10. 真实运行抓出的缺陷：Reviewer 被记了两次账

第三轮真实单题（§11）里 `state.usage` 报 **26 次调用 / ¥0.19581048**，而派发账本只有
**25 条 HTTP 请求 / ¥0.19192368**，差额恰好等于一次审查费用；事件流里同一次审查留下
两条 `llm_call`，role 分别是 `reviewer` 与 `review`。

根因：`PatchReviewer` 只想在"链路上没有统计层"时补一层，但判据写的是
`isinstance(provider, UsageTrackingProvider)`。冒烟链路把每个角色 provider 都包进
`_Override`（`--temperature` / `--max-output-tokens` 覆盖层），最外层就不再是统计层，
于是套上**第二层**——同一个请求 `add_call` 两次。审查是唯一会自己补统计层的 Agent，
所以正好落在 Reviewer 那一次调用上；前两次真实运行都没执行到审查，这条一直潜伏。

修法按**账本对象**查重：`llm/tracking.py:tracks_usage()` 沿 `inner` 逐层剥开，只要有
任何一层在往同一个 `Usage` 记就不再补第二层（带 `seen` 集合防环）。

负控制证据：把判据退回 `isinstance` 版本后，
`test_a_reviewer_behind_a_decorator_is_booked_exactly_once` 立刻失败在
`harness.usage.llm_calls - calls_before == 1`（实测为 2），修复后通过——这条用例真的
咬住了缺陷形状，不是跟着实现写的空断言。

## 11. 真实 GLM 单题实验（本轮恰好一次）

站点、run_id、派发账本全新，没有触碰 glm10～glm13 的任何产物；模型
`glm-5.3-flash-main`、官方 `swebench==4.1.0`、单实例单 seed 单重复、¥10 单题硬上限
（`--budget 10` 与 `CODEPILOT_DISPATCH_BUDGET_CNY=10` 两道独立强制）。
证据目录 `artifacts/phase11-glm14-20261020/`。

| 核对项 | glm14（2026-10-20） |
| --- | --- |
| 官方 verdict | **RESOLVED**（`patch_successfully_applied=true`，F2P `success`） |
| smoke 状态 | `SMOKE_COMPLETED_RESOLVED`，attribution `official_resolved` |
| 内部终态 / 级别 | `budget_exhausted` / `verification_level=budget_exhausted`，`stop_reason=budget_test_runs`（12/12 次测试执行用满） |
| 编码局部上限 | **未触及**：`tool_calls=10/30`，`coding_stop_reason=None` → DA-022 的真实同形状仍 `NOT OBSERVED` |
| 闭环读数 | validator 通过、目标测试 **PASS**、回归 L1/L2/L3 都执行但 `complete=false`（额度耗尽）、`new_failures=0` |
| 审查 | 第一轮补丁审过 1 次（`review_repairs=1` 触发一次修复）；**第二轮候选的审查未执行**，报告如实写 `unexecuted_stages=['regression_incomplete', 'review:the reviewer produced no result']`、`review_bound_to_candidate=false` |
| 预留读数 | 9 条预算检查事件带预留；开局 `tokens=56192 / cost=0.061338 / review_calls=2 / basis=reviewer-model:glm-5.3-flash-main`，进入修复轮后自动收敛为 `28096 / 0.030669 / 1` —— §9 的三件事（覆盖前置节点、按审查模型单价、欠账收敛）在真实运行里同时可见 |
| 阻断 | `exhausted` 判定 0 次、`blocked` 事件 0 条：费用 ¥0.196 远低于 ¥10，预留没有也不需要拦住任何东西 |
| 调用与费用 | 账本 26 次（含 §10 的多算 1 次）/ 真实 HTTP 25 次；输入 335,312、输出 9,749、缓存命中 174,976；账本 ¥0.19581048、账本口径真实花费 **¥0.19192368** |
| 派发账本 | `limit=10.0`、`frozen=false`、25 条全部 settled、单笔最大预留 ¥0.8114688、`cost_unknown_calls=0` |
| 墙钟 | 401.8 s |
| 容器清理 | `codepilot` 容器 0、卷 0；实例镜像 `a61197e0b294…` 按批准保留 |

结论口径按任务书分列，不互相替代：官方 `RESOLVED` ≠ 内部 `m2_verified`（本次内部级别是
`budget_exhausted`），Reviewer 结论只约束它绑定的那一版补丁。真实实验**只有一次**，
没有为了追 30 次工具上限那条路径而重复付费。

`max_test_runs=12` 在"两轮修复 + 每轮 L1/L2/L3"这种形状下先于费用耗尽，是本轮真实
运行暴露的下一个约束（§12 风险 1）。

## 12. 本轮门禁与残留风险

| 门禁 | 命令（WSL2 Ubuntu-24.04，`.venv`） | 结果 |
| --- | --- | --- |
| 静态 | `ruff check .` | All checks passed，exit **0** |
| 格式 | `ruff format --check .` | 385 files already formatted，exit **0** |
| 类型 | `mypy src` | no issues found in 170 source files，exit **0** |
| 全量离线（CI 同参） | `pytest -m "not docker and not llm" --cov --cov-report=term` | round 4：**3407 passed / 6 skipped / 48 deselected**，exit **0**，510.83 s，覆盖率 **91%**（阈值 80%，未下调）；round 5 在最后一次"测试文件内一处空白折叠"之后重跑同一命令，结果见 §13 |
| 真实 Docker | `pytest -s -m docker --no-cov` | **48 passed / 0 failed / 3413 deselected**，exit **0**，326.56 s（新增 2 项真实容器闭环用例） |
| 779 条生产链路 | `scripts/verify_baseline_nodeids.py` | 25 条判据全 PASS，exit **0**，16.36 s，模型调用 0 次 |

6 个 skip 仍是既有的 Windows 专属用例与 `CODEPILOT_TEST_EMBEDDING_MODEL` 未设置；
**没有新增 skip、没有降低阈值、没有删改断言**。

风险与边界：

1. **`max_test_runs=12` 是本轮真实运行的实际约束**：两轮迭代各跑 L1/L2/L3 就把额度用满，
   第二轮候选的审查因此未执行（如实报告）。测试次数不在预留内（§2 的既有口径），要改变
   这一条得先决定"审查优先"还是"回归优先"，属于政策选择而非本轮缺陷修复。
2. 预留的 token 常数仍然假设审查上下文**全部不命中缓存**（偏保守）；若服务商按上下文
   长度阶梯计价，24k 档单价与真实 30k+ 档可能不同。本轮真实审查实测一次约
   `in 24k / out 4k` 量级，费用 ¥0.003887，与预留 ¥0.061338（两次）方向一致且余量充足。
3. 真实同形状（Coder 触顶 30 次）仍未在真实模型上观察到 → DA-022 的
   `REAL SAME-SHAPE NOT OBSERVED` 保持不变。
4. §10 的重复计费修复发生在真实运行**之后**：glm14 的 `state.usage` 数字仍带着那次
   多算（证据未改写），事后核对请以派发账本（HTTP 真值）为准。
5. test50/test300 批量、10 题费用校准、GitHub Actions 仍 **NOT RUN**（DA-017 / DA-019 不变）。

## 13. 最后一次空白折叠之后的复验

`ruff format` 在 round 4 之后折叠了 `tests/graph/test_phase11_closure.py` 里一处换行
（该文件不带 docker 标记，纯空白）。为免"报告数字与最终树不符"，静态三项 + 全量离线
在同一最终树上重跑一轮（round 5）：

| 门禁 | 结果（round 5，最终树） |
| --- | --- |
| `ruff check .` | All checks passed，exit **0** |
| `ruff format --check .` | 385 files already formatted，exit **0** |
| `mypy src` | no issues found in 170 source files，exit **0** |
| `pytest -m "not docker and not llm" --cov --cov-report=term` | **3407 passed / 6 skipped / 48 deselected**，exit **0**，348.55 s，覆盖率 **91%**（与 round 4 同一组分母） |

真实 Docker 集合不重跑的理由如上（唯一 delta 是非 docker 测试文件的空白折叠），
round 4 的 **48 passed / 0 failed / exit 0 / 326.56 s** 仍是该集合的证据。
