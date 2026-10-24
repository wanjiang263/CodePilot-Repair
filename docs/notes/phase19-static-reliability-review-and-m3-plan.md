# Phase 19：静态可靠性复审与 M3 下一轮方案

日期：2026-10-24  
基线：`58f7a91`（工作开始时的 HEAD；工作区无 tracked/staged 改动）  
本轮性质：**只做静态分析、源码修改、测试编写和本地提交**。未运行 pytest、ruff、mypy、编译、构建、Docker、Harness 或任何模型请求；未 push，未创建 PR。

---

## 0. 结论先行

1. a12/a18/a21 都不是“模型没有给出足够好的补丁”这一条能解释完的：
   - **a12** 的 Reviewer 上下文没有包含补丁，模型照抄 issue 里的 pmxbot 路径，Harness 在证据校验时抛 `ValueError`；这是上下文/归因缺陷。
   - **a18** 的 L2 实际是 `368 passed, 0 failed, exit_code=0`，却因 pytest 可选依赖被重新分类成 `FAIL`，回归判成“状态与逐测试结果矛盾”；这是状态分类缺陷。
   - **a21** 的 168 个目标测试按**字典序**展开，导致 `test_move_dir` 先于 `test_visit_*` 执行，`sampledir` 已被移走，8 个基线 PASS 的测试变成假失败；这是目标展开顺序缺陷。
2. 三次尝试的 candidate 与 delivered 补丁**文本相同**，但 candidate 用规范化 diff 的 SHA-256、delivered 用原始字节 SHA-256；旧 acceptance 直接把两者比较，必然得到 `candidate_equals_delivered_patch=false`。这是补丁身份口径缺陷，不是模型改写了补丁。
3. 本轮已修复上述可静态证明的缺陷，并补上 acceptance/official 的身份绑定、人工 recheck 收紧、checkpoint 身份拒绝、Reproducer/Coder 写权限分离、预算 opening 校验和 registry 单调性检查。
4. 新增测试**已编写、未执行**。因此“修复后不变量”仍属于待运行验证主张；本文不声称测试通过，也不声称达到发布条件。
5. pytest P2 仍为 **NOT VERIFIED**；pytest 官方评测仍为 **NOT_EVALUATED / DEFERRED**；未派发 a22。

---

## 1. 工作区收口

| 项 | 事实 |
|---|---|
| HEAD | `58f7a91`，与上一轮核实一致 |
| tracked/staged | 工作开始时无 tracked 修改、无 staged 改动 |
| `tests/graph/test_phase9_regression.py` | 索引与工作区均为 LF，`git diff` 为空；**不存在待处理的行尾差异** |
| `.gitattributes` | 仓库没有 `.gitattributes`；`core.autocrlf=false` |
| 行尾现实 | 仓库既有文件混合 CRLF/LF（例如 `checkpoint.py`、`regression.py` 为 CRLF，`test_phase9_regression.py` 为 LF）。本轮编辑保留各文件原有行尾，不做全仓归一化 |
| 空白检查 | `git diff --check` 会把既有 CRLF 的 `\r` 报成 trailing whitespace；使用 `git -c core.whitespace=cr-at-eol diff --check` 为 0 |
| 未跟踪文件 | 173 个根目录诊断文本、`artifacts/`、缓存/临时目录等均保留；本轮未删除。具体保留项见 §9 |

不新增 `.gitattributes`：没有证据表明跨平台反复产生差异是当前主要问题；加全仓 `text=auto` 会制造一次大规模重规范化，超出最小修复范围。

---

## 2. a12 / a18 / a21 证据驱动复盘

证据摘要：`docs/evidence/phase19/static-review-evidence.json`（来源路径、来源 SHA-256、关键字段与短摘录已一并提交）。a12/a18 的 `runs/*/report.json`、`patch_final.diff` 是本地 evidence，**未提交**；它们对应的根报告 `reports/*.json` 已提交，摘要中同时记录了本地来源哈希。a21 的 run report / patch final 已提交。

### 2.1 紧凑对照表

| 维度 | a12 | a18 | a21 |
|---|---|---|---|
| run_id | `...a12...dfa7aa7a` | `...a18...31ccfbc9` | `...a21...6f621582` |
| base commit | `2f7415cf...` | 同左 | 同左 |
| 模型 / temperature | glm-5.3-flash-main / **0.0** | 同 / **0.0** | 同 / **0.2** |
| 代码版本 | `0.1.0+d5734ad-dirty` | 同 | 同 |
| 测试次数上限 | 18 | 18 | 18 |
| 最终状态 | `error/workflow_error` | `failed/tests_failed` | `stopped/repeated_patch` |
| 目标测试 | planner，7 项（含文件目标）→ 实际 174 | planner，6 项 | planner，文件目标 → 实际 168（字典序） |
| L1 | PASS，174/0 | PASS，6/0 | **FAIL，160/8** |
| 回归 | full complete，168/0，new=0 | **NOT RUN**，L2=368/0 但 status=FAIL | 未执行 |
| 复现 | reproduced，2 次 | **failed/invalid_pass**，4 次 | reproduced，3 次 |
| Reviewer | 调用 1 次，随后 Harness ValueError | 未执行 | 未执行 |
| candidate（规范化） | `ece99621...` | `09e0b866...` | `9b2eed07...` |
| delivered（原始字节） | `9b8b599f...` | `e284a4d7...` | `4b7f04c3...` |
| candidate == canonical(delivered) | **true** | **true** | **true** |
| 旧 acceptance 的 candidate==final | false | false | false |
| 官方评测 | DEFERRED | DEFERRED | DEFERRED |

### 2.2 a12：Reviewer 看不到补丁，证据校验把 harness 拖入 error

链条（来源）：

- 输入/配置：根报告 `pytest-dev__pytest-11148-a12/reports/...json` 的 `runtime`：模型 glm-5.3-flash-main、temperature 0.0、`max_output_tokens=4096`；`budget`：`max_test_runs=18`、`max_runtime_seconds=1800`、`max_cost=16.42608`。
- 复现：run report `reproduction.status=reproduced`，`kind=valid_assertion`；第 1 次候选 `invalid_no_assertion`，第 2 次命中 `AssertionError`。
- planner / coder：`closure.target_test_source=planner`；`attempts[1].patch_diff` 与 `patch_final.diff` 文本相同。
- validator / target / regression：`closure.validator_passed=true`、`target_tests_status=PASS`、`regression_complete=true`、`regression_levels=[L1_target,L2_impact,L3_full]`、`regression_new_failures=0`。
- reviewer：`events.jsonl` 第 109 行有一次 reviewer `llm_call`（3098 in / 515 out），第 110 行 `node_end` 为 `error: review: [harness_internal_error/harness] ValueError: review evidence references an unverified file`，`corrections_used=0`。
- 具体引用：`llm_calls.jsonl` 中 `purpose=reviewer` 的响应 JSON 的 `issues[*].file` 全部是 `tests/unit/test_commands.py`（pmxbot 项目的路径），`reviewed_patch_hash=""`、`reviewed_iteration=0`；该请求只有一个 user message，内容中**没有** `diff --git` 或工作区路径（证据摘要 `reviewer_call.prompt_contains_diff=false`）。
- 停止原因：首次审查调用的输出在证据校验阶段被拒绝，没有进入“纠正重问”；也不是候选/最终补丁解析失败。
- 哈希：candidate `ece99621...`、final raw `9b8b599f...`、`canonical(final)==ece99621...`、`candidate_equals_delivered_canonical=true`；reviewed 为空。
- 旧 acceptance：`review_executed=false`、`review_bound_to_candidate=false`、`reviewed_patch_hash=null`，因此 REJECTED；这个拒绝本身是正确的，但拒绝原因被写成 `harness_internal_error`，且旧代码没有把“Reviewer 没看到补丁”作为模型侧可纠正错误处理。

当前代码核对：

- `src/codepilot/agents/reviewer.py:62-83` 现在把 `patch`、`plan`、`impact`、`validation`、执行/回归/attempts 和改动文件源码放进 `facts`，并明确要求只引用仓库真实文件；`validate_evidence` 对不存在路径抛 `AgentFailureError(model_output_invalid)`，`complete_structured(max_attempts=2)` 给出一次纠正机会。历史触发条件（无补丁上下文 + 照抄 issue 路径）在静态上已被覆盖，但**未运行验证**。

### 2.3 a18：L2 全量执行了，却被“可选依赖”重新归类为 FAIL

链条（来源）：

- 配置：同 a12，temperature 0.0；`max_cost=13.776888`。
- 复现：`reproduction.status=failed`、`kind=invalid_pass`、`attempts=4`；第 4 次在 base 上直接 PASS，因此没有被认定为有效复现。旧 acceptance 的 `reproduce_recorded` 仍为 true（它只要求有状态/原因），不能读成“复现成功”。
- planner/coder/validator：target 6 项 PASS；candidate `09e0b866...`，最终 raw `e284a4d7...`，文本相同。
- L2：`regression/1/L2.json` 为 `status=FAIL`、`exit_code=0`、`tests_passed=368`、`tests_failed=0`、`tests=[368 passed, 17 skipped]`。`stdout_tail` 显示 `368 passed, 15 skipped, 2 xfailed`，其中一个 skip 是 `testing/test_monkeypatch.py::test_issue1338_name_resolving: could not import 'requests'`。
- 根因：`sandbox/status.py:69-73` 的 `is_third_party_dependency_error` 从诊断文本识别 `No module named 'requests'`，把执行判为 `ENV_ERROR`；`sandbox/official.py` 的 `_reclassify_pytest_fixture_dependency` 再把 pytest 实例的 `ENV_ERROR + 有 tests` 一律改成 `FAIL`，没有看 `exit_code==0` 且没有 failed tests。于是 `regression.py:159-163` 判为“状态与逐测试结果矛盾”，L2 不进 L3，`complete=false`。
- 旧 acceptance：`regression_clean=false`、`regression_new_failures=0`、`review_executed=false`，REJECTED。拒绝结论正确，但 L2 被误判为 FAIL 使 `failure_reason` 指向“矛盾”，掩盖了真实原因。

当前修复：`_reclassify_pytest_fixture_dependency` 现在仅在 `exit_code != 0` 或存在 failed/error 时给 `FAIL`；`exit_code==0` 且无失败测试时给 `PASS`，并在 `diagnostics` 记录 `reclassified_from` / `reclassify_reason`。新增测试 `tests/unit/sandbox/test_phase19_official_boundaries.py`（已编写、未执行）。

### 2.4 a21：文件目标按字典序展开，8 个假失败

链条（来源）：

- 配置：temperature 0.2；`max_cost=12.638821`。
- attempts：4 次。`attempts[0].patch_diff=""`（哈希 `e3b0c442...`）；`attempts[1]` 两文件，168 目标中 159 passed/9 failed；`attempts[2]`、`attempts[3]` 补丁完全相同（`9b2eed07...`），`attempts[1]` 为 `8a0d819a...`。`events.jsonl` 第 153 行 `check_budget` 因 `repeated_patch` 停止，第 156-158 行 finalize 写 `stopped/repeated_patch`。
- 目标：文件 `testing/_py/test_local.py` 被展开成 168 个 NodeID，顺序是 `TestBinary...`、`TestExecution...`、`TestImport...`、`TestLocalPath...` 的**字典序**（证据摘要 `target.first_five`）。
- 失败：最终 target run 160 passed/8 failed；8 个全是 `TestLocalPath::test_visit_*`，消息一致地缺少 `sampledir` / `sampledir/otherfile`。基线中这 8 个全部为 `passed`（证据摘要 `baseline_failed_for_failures`）。
- 根因证据：`a10-order.txt`（SHA-256 `e314bd5b...`）显示 `test_visit_*` 依赖 `sampledir`；`a10-line448.txt`（`119d51d3...`）第 12-33 行显示 `test_move_dir` 把 `sampledir` 移到 `moveddir` 且不恢复；`a10-regression.txt`（`0755240a...`）显示同一批节点在旧 attempt 中 `passed -> failed`。按 pytest 收集顺序，`test_visit_*` 在 `test_move_dir` 之前；按字典序则相反。
- 代码根因：`TestBaseline.outcomes` 在 `sandbox/baseline.py:50` 被 `sorted(...)`；`target_tests._expand_file_target` 之前直接遍历这个 dict，于是丢失了 `run_outcomes[0]` 的收集顺序。
- 补丁身份：candidate `9b2eed07...`、final raw `4b7f04c3...`、`canonical(final)==9b2eed07...`；最终交付补丁与最后一次候选文本相同。
- Reviewer：未执行；`review_high_issues=0` 是报告字段默认值，不是审查通过。旧 acceptance 的 `review_executed=false`、`target_test_passed=false`、`regression_clean=false`，REJECTED 正确。
- `repeated_patch` 判定输入：iteration 4 与 iteration 3 的规范化补丁哈希相同；空补丁只出现在 iteration 1，不是停止原因；iteration 状态与哈希一致。
- finalize/报告绑定：本实例未出现交付过滤，candidate/final 文本一致；旧 acceptance 的不一致完全是哈希口径不同。但 `eval/assembly.py` 原先把 `state.current_patch` 当最终补丁，若 finalize 过滤了未审查的新测试，prediction 可能与 `patch_final.diff` 不同——本轮已改为优先读取 `patch_final.diff`。

当前修复：

- `target_tests._baseline_order` 按 `run_outcomes` 的插入顺序（pytest 收集顺序）展开，再追加后续运行/字典中未出现的节点；文件目标只把基线 PASS 且非 flaky 的节点作为 L1，基线失败节点单独记录在 `TargetTestSelection.baseline_failures` / `Attempt.target_test_baseline_failures` / `VerificationClosure.target_test_baseline_failures`。
- `regression.fixed_tests` 现在记录“base 失败、patch 后通过”的节点，作为独立于 L1 目标选择的 Issue 修复证据。
- 新增测试 `tests/graph/test_phase19_static_reliability.py`（已编写、未执行）。

### 2.5 跨尝试比较：哪些变化来自系统，哪些来自模型

| 变化 | 证据 | 结论 |
|---|---|---|
| a12 → a18 → a21 的代码版本字符串相同 | 三个 `checkpoint_manifest.json` 都是 `0.1.0+d5734ad-dirty` | 不能从 artifact 区分“哪一次系统修改”；`dirty` 不含内容哈希 |
| a21 只把 temperature 从 0.0 改成 0.2 | 根报告 `runtime.temperature` | 这是运行配置差异，不是系统能力差异 |
| a12 Reviewer 无补丁上下文 | reviewer call 请求无 `diff --git` | 系统上下文缺陷；不代表模型审查能力 |
| a18 L2 状态误分类 | L2 exit 0 / 368 passed；reclassify 代码 | 系统状态缺陷；不能据此说补丁导致回归 |
| a21 目标顺序导致假失败 | target 字典序 + move_dir/visit 依赖 | 系统目标展开缺陷；8 个失败不是模型引入 |
| 三次 candidate/final 文本相同、哈希不同 | canonical 计算 | 系统身份口径缺陷；验收 false negative |
| 模型补丁内容不同（a12 sys.modules；a18 同族；a21 两文件） | attempts `patch_diff` | 模型输出不同；**没有对照实验**，不能建立“系统修改导致修复质量变化”的因果 |

不将 a12 的 target、a18 的 regression、a21 的 review 拼成一次成功闭环；三者是三次独立、未通过的运行。

---

## 3. 本轮确认并修复的缺陷

严重程度按实际影响：**S1 = 可让验收/预算/补丁绑定产生错误结论；S2 = 会让诊断、恢复或证据解读错误；S3 = 一致性与可维护性**。

| ID | 严重度 | 确认缺陷 | 修复 | 测试（未运行） | Commit |
|---|---|---|---|---|---|
| D-01 | S1 | candidate（规范化哈希）与 delivered（原始哈希）直接比较，必然 false negative；a12/a18/a21/a7 都出现 | `l2cal10e_acceptance.py` 用 `patch_hash(run.git_diff)` 与 candidate 比较；另增 `delivered_patch_matches_report`、`validated_patch_bound`、`target_tested_patch_bound`；`RunReport`/`InferenceOutcome` 增规范化交付身份 | `test_candidate_and_delivered_hashes_are_compared_in_the_same_normal_form` | `31f7d68` |
| D-02 | S1 | 未执行 review 时 `review_high_issues=0` 可被误读为“零问题”；旧 acceptance 还不检查 `review.approved` | `VerificationClosure.review_high_issues_known`；acceptance 要求 review executed + known + high=0 + approved=true + hash 绑定 | `test_unexecuted_review_has_unknown_high_issue_count`、`test_review_of_a_different_patch_is_rejected` | `254a8b2` / `31f7d68` |
| D-03 | S1 | 文件目标按 baseline 字典序展开，破坏 pytest 收集顺序（a21 8 个假失败） | `target_tests._baseline_order` 使用 `run_outcomes` 顺序；基线失败节点显式披露 | `test_file_target_expansion_keeps_pytest_collection_order` | `254a8b2` |
| D-04 | S1 | pytest 可选依赖被重新归类为 `FAIL`，a18 L2 的 368 passed/0 failed 被判“矛盾” | `_reclassify_pytest_fixture_dependency` 在 exit 0 且无失败测试时给 PASS | `test_all_skipped_exit_zero_is_pass_for_pytest_optional_imports` | `579abe2` |
| D-05 | S1 | L3 无候选时可在没有真实 L2 全量覆盖的情况下标 complete；L3 全量覆盖未检查 unexecuted | `regression.py` 要求 L2 存在、PASS、无 unexecuted、覆盖全部授权节点、授权列表无重复；全量 L3 要求 unexecuted 为空 | `test_l3_completion_requires_l2_to_cover_the_authorized_baseline`、`test_l2_missing_one_authorized_node_is_not_complete` | `254a8b2` |
| D-06 | S1 | official 的 recheck 只要求 `accepted=true` + 同 run_id，任意 JSON 可放行；且不校验 prediction 字节 | recheck 必须是 schema 2，绑定 instance/label/run_id/run_report/candidate/final/delivered/reviewed/全部 checks；official 校验 prediction jsonl SHA、manifest、model、instance 和 patch SHA | `test_recheck_must_be_schema_two_and_bind_the_same_hashes`、`test_bare_same_run_recheck_is_not_a_bypass`、`test_official_gate_rejects_a_prediction_with_different_patch_bytes` | `31f7d68` |
| D-07 | S1 | acceptance 用 mtime 选择 attempt；多候选时静默取最新 | 取消 mtime；`--run-id` 显式身份；无 run_id 时只允许唯一匹配，多候选直接拒绝 | `test_ambiguous_reports_require_an_explicit_run_id` | `31f7d68` |
| D-08 | S1 | 单题 opening 可由脚本硬编码，未与 manifest 份额绑定；schema 1 无实例归属仍可用于新派发 | `instance_opening_cny` 只接受 schema 2 并重算份额；`DispatchBudget` 在首次请求前校验 manifest SHA/实例/金额；`batch_budget` 拒绝 schema 1 的 per-instance 派发；统一入口从 batch guard 读取 `instance_opening` 并写入 source | `test_instance_opening_is_the_manifest_share_not_the_batch_total`、`test_per_instance_dispatch_rejects_a_wrong_opening_before_any_request`、`test_schema_one_manifest_can_be_read_but_not_used_for_instance_opening` | `579abe2` |
| D-09 | S1 | registry 可以在同名 key 上把已结算金额改小；丢失 ledger 时只挡了“总额>0”一种形状 | `record_attempt_from_ledger` 拒绝 settled 下降；unknown 仍开放时拒绝上界下降；缺文件/换路/哈希/同键覆盖沿用现有 fail-closed | `test_registry_refuses_to_lower_settled_spend`（已有 missing-ledger 测试保留） | `579abe2` |
| D-10 | S2 | checkpoint 恢复不读 manifest；schema/base/run_id 不兼容时仍继续 | `resume_run` 在读 state 前检查 manifest，不兼容抛 `CheckpointIdentityError`；base 与 state 再核对 | `test_schema_or_run_identity_mismatch_is_rejected` 等 | `8744072` |
| D-11 | S2 | Coder 与 Reproducer 共用 `repro_write=True` 的 policy，确认后的复现证据可被 Coder 改写 | assembly 给 Coder/Planner 的 policy `repro_write=False`，给 Reproducer 单独的 trusted policy；Coder 仍可读 | `test_coder_policy_cannot_edit_confirmed_reproduction_test` 等 | `31f7d68` |
| D-12 | S2 | COLLECTION_ERROR 归因用路径/模块子串，可能把无关堆栈里的 `path.py` 提到补丁头上 | `_target_execution_attribution` 只认 traceback/collection 的结构化文件帧；无匹配时明确 UNKNOWN/基础设施，不归模型 | `test_collection_attribution_uses_frames_not_substrings` | `254a8b2` |
| D-13 | S2 | `acceptance.official_evaluation` 的 `DEFERRED` 容易被读成最新官方 verdict | acceptance schema 2 增加 `official_evaluation_semantics=deferred_at_acceptance`；internal 输出同样标注；历史文件不改 | 由 `test_candidate...` 的 evidence 断言覆盖（未执行） | `31f7d68` |
| D-14 | S3 | Planner `_test.py` 分支不可达（`is_test_path` 已包含 `_endswith('_test.py')`） | 删除死分支，统一走 `is_test_path` | 由现有 Planner 语义测试覆盖（本轮未新增） | `254a8b2` |
| D-15 | S3 | finalize 可能过滤未审查的新测试，inference 仍拿 `state.current_patch` 当交付补丁 | `assembly._outcome` 优先读 `patch_final.diff` | 由 acceptance identity 测试间接覆盖（未执行） | `31f7d68` |

### 3.1 未改动的安全边界复核

- tar 提取：`member.isfile()` 排除 symlink/hardlink；`..`、绝对路径、Windows drive/UNC、symlink 父目录都经 `target.resolve().is_relative_to(workspace)` 拒绝。本轮新增 `tests/unit/sandbox/test_phase19_official_boundaries.py` 覆盖 traversal、symlink 父目录、symlink/hardlink 成员；**未运行**。`_extract_single_file` 的目标是固定路径 `workspace/src/_pytest/_version.py`，不接收成员路径。
- git exclude：`_git_exclude_path` 若解析到 workspace 外（linked worktree 的外置 gitdir）会 `OfficialImageError` 拒绝写入；本轮未改。
- Reviewer 归因：`AgentFailureError` 保留 `failure_class=model_output_invalid`、`origin=model`、`corrections_used`；provider/timeout 走 `classify_exception`，不会被包成语义错误。重问仍受 `max_attempts=2` 和 review 预算约束。

---

## 4. 遗留项处理表

| 原编号 | 当前是否存在 | 影响 | 实际处理 | 新增测试 | 未运行状态 | Commit |
|---|---|---|---|---|---|---|
| L-01 单题 opening / manifest 份额 | 是 | S1：可能少算/错算遗留费用 | schema2 份额函数；DispatchBudget 请求前校验；batch_budget 拒绝 schema1 per-instance；统一入口传 source | 3 项 budget binding 测试 | NOT RUN | `579abe2` |
| L-02 schema 1 recovery 兼容 | 是 | 读取历史可以，授权新派发不可以 | schema1 仍可读取/校验（并重算源账本）；新 per-instance 派发拒绝 schema1 | `test_schema_one...` | NOT RUN | `579abe2` |
| L-03 registry record 失败退出 | 是（统一入口） | S1：账本已结算但 registry 未写仍继续验收 | `run_l2cal10e.sh` 检查 record 退出码，失败写 `STOP_REGISTRY_RECORD` 并停止；a10-a21 历史脚本保持原样 | 无（shell 路径需运行验证） | NOT RUN | `579abe2` |
| L-04 acceptance 报告选择 | 是 | S1：mtime 冒充身份 | 取消 mtime；显式 `--run-id`；多候选拒绝；official 共用同一 run 身份 | `test_ambiguous...` | NOT RUN | `31f7d68` |
| L-05 旧 aggregate 脚本 | 存在但不在任何当前脚本/文档命令中自动调用 | S3：其 `acceptance-recheck` 读取口径会把旧人工 recheck 当通过 | 保留原件与 a9 历史用途，不覆盖输出；本轮不执行；M3 汇总改用 strict acceptance + manifest 份额（见 §7） | 无 | NOT RUN | 未提交（保留项） |
| L-06 复现测试编辑权限 | 是 | S2：Coder 可改写复现证据 | Coder/Planner `repro_write=False`；Reproducer 独立 trusted policy；读权限不变 | 3 项 permission 测试 | NOT RUN | `31f7d68` |
| L-07 collection error 归因 | 是 | S2：子串误归因 | 只认结构化 traceback/collection 文件帧；无帧则 UNKNOWN/基础设施 | 1 项 attribution 测试 | NOT RUN | `254a8b2` |
| L-08 acceptance `official_evaluation` | 是 | S2：DEFERRED 被当官方结论 | schema 2 semantics 字段；旧字段保留；历史文件不改；消费者检查见 §8 | acceptance evidence 断言 | NOT RUN | `31f7d68` |
| L-09 官方 recheck 例外 | 是 | S1：任意同 run JSON 可放行 | schema 2 + 全哈希绑定；旧 sklearn a7 recheck 保留但不再作为新派发通用凭证 | 3 项 official gate 测试 | NOT RUN | `31f7d68` |
| L-10 Planner `_test.py` 分支 | 是（不可达） | S3：两套目标验证口径 | 删除死分支 | 现有语义测试 | NOT RUN | `254a8b2` |

---

## 5. 尚未证实的假设与待验证项

1. **修复后的 Reviewer 上下文确实包含补丁**：代码静态上把 `patch` 放进 facts，但没有对真实上下文预算做运行验证；大补丁可能在 `ContextBuilder` 预算下被降级。最小验证：构造大补丁，断言 `review` 请求的 user message 含 `diff --git` 和变更文件路径。
2. **a18 同形状的 L2 会变成 PASS**：需要真实 pytest 实例重放；本轮只写了纯函数测试。
3. **a21 顺序修复会消除 8 个假失败**：需要同一 base/patch 的 Docker 重放；本轮只验证了展开顺序。
4. **schema 1 的历史 manifest 全部能通过加严后的源账本重算**：已对仓库内 schema1 样本做静态扫描，但没有逐个执行 `validate_recovery_manifest`；最小验证：对 `artifacts/**/recovery-manifest*.json` 做只读校验。
5. **人工 recheck 的信任边界**：schema 2 仍是一个文件；能写该文件的人可以复制真实哈希。它阻止“只写 accepted=true”，但不提供密码学签名。若需要更强保证，应改为由独立签名密钥或 append-only 记录生成；本轮不引入密钥。
6. **checkpoint `code_version` 差异的处理**：当前只硬拒绝 schema/run/base 不匹配；`d5734ad-dirty` 不含内容哈希。是否要把代码版本不匹配也判为不可恢复，需要产品选择。
7. **旧 aggregate 的最终汇总**：未新增汇总入口。M3 的统计口径已在 §7 定义；若仍需要历史汇总，应在批准后新增带版本号的脚本，输出到新文件，不覆盖旧输出。

---

## 6. 定向测试清单（全部 NOT RUN）

本轮没有执行任何测试。以下命令仅作为下一轮（需单独运行授权）的精确清单；通过标准是“全部通过且不新增 skip”。

```bash
# 1) 目标展开顺序、L2/L3 完成、collection 归因、review 绑定
.venv/bin/python -m pytest -q \
  tests/graph/test_phase19_static_reliability.py \
  tests/unit/graph/test_phase9_report.py

# 2) acceptance / official 身份绑定与 recheck
.venv/bin/python -m pytest -q \
  tests/unit/test_phase19_acceptance_gate.py

# 3) 预算 opening / registry 单调性
.venv/bin/python -m pytest -q \
  tests/unit/llm/test_phase19_budget_binding.py \
  tests/unit/llm/test_dispatch_recovery.py

# 4) pytest 可选依赖状态与 tar 边界
.venv/bin/python -m pytest -q \
  tests/unit/sandbox/test_phase19_official_boundaries.py \
  tests/unit/sandbox/test_baseline.py

# 5) checkpoint 身份与复现写权限
.venv/bin/python -m pytest -q \
  tests/unit/graph/test_phase19_checkpoint_identity.py \
  tests/unit/tools/test_phase19_repro_permissions.py
```

通过标准：

- 上述 5 组全部通过；
- 新增测试无 skip；
- 不修改断言、不删除测试、不放宽 acceptance 六项；
- 若某组失败，先判断是测试夹具问题还是实现问题，修复后重新运行；不得用 `--deselect` 隐藏失败。

此外，下一轮运行前应做一次只读静态复核：`git show` 的四个修复 commit、`git diff --check`（`cr-at-eol`）、以及 acceptance/official 的 hash 口径。

---

## 7. M3 下一轮方案（拟议，未授权）

### 7.1 样本策略：调试集、固定评测集、消融集分离

- **调试集（debug set）**：`pytest-dev__pytest-11148` 已经被反复调试（a3–a21），它是**工程调试样本**。后续再跑只能作为“系统修复是否覆盖已知触发条件”的调试证据，不能当作独立、未见样本的泛化证据，也不进入 M3 主指标的分母/分子。
- **固定评测集（fixed eval set）**：在运行前提交一份冻结的实例清单，候选池为 SWE-bench Lite test split；排除所有已经出现在 `artifacts/phase18/**` 的 instance_id，且每个仓库最多取 1 个，按 instance_id 排序取前 N。清单、文件 SHA 和数据集 revision 必须在运行前提交；运行中不改。
- **消融集（ablation set）**：从固定评测集中预先注册子集，用于比较“有无某个系统修复/是否开启 Reviewer/是否全量回归”等；不与主指标混算。
- **分母规则**：没有补丁、补丁非法、内部验收失败、环境无效都留在“计划实例数”分母中。不得因为“没有补丁”或“内部失败”把实例从任务成功率分母移除。
- **单独报告**：
  1. 有效官方判分率 = `RESOLVED / (RESOLVED + UNRESOLVED)`，只统计真正拿到官方判分的实例；
  2. 任务级解决率 = `RESOLVED / 计划实例数`（含未评测、内部失败、环境失败）；
  3. 内部闭环通过率 = `ACCEPTED / 计划实例数`；
  4. 环境无效率 = `ENV_ERROR/SANDBOX_ERROR/COLLECTION_ERROR(基础设施) / 计划实例数`。
- 主指标不使用“内部 claims success”，不使用 gold patch / FAIL_TO_PASS / PASS_TO_PASS 指导推理。

### 7.2 代码冻结点、配置与随机性

- **冻结点**：Phase 19 文档 commit 后的 HEAD。运行前记录 `git rev-parse HEAD`、`git status --porcelain` 必须为空；运行中禁止改代码、测试或配置。任何修改都使该批结果不再属于同一版本。
- **模型**：glm-5.3-flash-main（与历史一致）；若更换模型，必须单独作为一批并明确标注，不与本批合并。
- **temperature**：主批 `0.0`；若需要采样，单独登记为 `temperature=0.2` 的调试/消融批，不能混在主指标中。
- **随机性/重复**：每个实例 1 次主运行；`seed` 固定并写入报告。重试必须使用新 label + 新 ledger；旧账本不重写。主指标按“每实例 1 次”统计，不挑最好的一次。
- **停止条件**：`max_iterations=5`、`max_test_runs=18`、`max_runtime_seconds=1800`、`max_tool_calls_per_iteration=30`、`max_review_calls=2`、单题费用上限、批次费用上限。预算/未知费用/连续基础设施失败立即停批。
- **费用继承**：新 M3 批次建立新的 manifest/ledger；历史未知预留只在 manifest 中作为 opening 计入，不重置、不按零、不重复相加。历史剩余额度**不自动变成**新一轮费用授权。

### 7.3 拟议运行规格（等待用户书面批准）

**实例数与选择**（拟议）：

- 固定评测集 6 个实例；仓库多样性优先；实例清单在运行前冻结并提交。
- 调试集 pytest 1 个实例只做回归调试，不进入 6 个实例的主指标。
- 不默认派发 pytest a22。

**预算**（拟议，不是现有授权）：

- 批次硬上限：`CNY 30.00`；
- 单实例硬上限：`CNY 4.00`；
- 未知费用预留：沿用 manifest 的保守上界；若同一批次引用历史未知预留，则 opening 先占用拟议上限，剩余额度才是新花费；
- 最小可派发余额：`CNY 0.05`；
- 任何 unknown/frozen ledger 先停批，不自动重试。

**允许的运行范围**（批准后才可执行）：

1. `scripts/l2cal10e_preflight.py` 对该批 catalog 的只读预检（镜像、环境、快照）；
2. 每个实例的 `scripts/l2cal10e_internal.py`（真实 Docker、模型、记录 LLM）；
3. 仅对 `ACCEPTED` 的实例运行 `scripts/l2cal10e_official.py`；
4. 网络只允许模型 API 与 SWE-bench 镜像/数据集所需通道；不允许访问 gold 字段；
5. 不运行 a22，不运行 M4/test50/test300，不运行旧 aggregate 脚本。

**注意**：当前没有一个已审查的 M3 批处理入口。批准本方案后，下一步应是先实现并静态复审一个参数化的 M3 入口（复用 strict acceptance/official、manifest opening、registry record 失败退出），再申请实际运行；本轮不实现、不运行该入口。

**预期产物**：

- 每实例：workspace identity、report.json/report.md、attempts、patches、junit、events、ledger、acceptance、official-evaluation（仅 accepted 时）；
- 批次：冻结 catalog、manifest、attempts registry、batch status、费用 reconciliation、固定口径的结果表（区分有效官方判分率/任务级解决率/内部闭环通过率/环境无效率）。

**进入正式评测的条件**：

1. 该批代码冻结且工作树干净；
2. 定向测试清单全部通过（§6）；
3. 预检镜像/数据/环境全部通过；
4. manifest 的 opening 与实例份额通过 `instance_opening_cny` 校验；
5. acceptance 对每个实例重算通过，且 prediction 字节与 delivered patch 一致；
6. 官方 Harness 显式执行并产生 `RESOLVED/UNRESOLVED`；否则记为 `NOT_EVALUATED`，不得用内部结果替代。

---

## 8. 仓库交付与证据可复核性审计

已提交且可复核：

- registry 引用 25 个 ledger：**25/25 已提交**，本地 SHA-256 与 registry 全部一致；
- 受保护文件：`preflight/protected-before.sha256` 12 条，**12/12 已提交**，当前哈希 **12/12 一致**；
- a12/a18/a21 的根报告、acceptance、dispatch ledger：已提交；
- a21 的 run report / patch_final / checkpoint manifest / plan artifacts：已提交；
- a12/a18 的 **run report 与 patch_final 未提交**；它们的关键字段已脱敏提取到 `docs/evidence/phase19/static-review-evidence.json`，但原始 reviewer 请求、events、llm_calls 仍未交付。结论“a12 Reviewer 无补丁上下文”目前可由本地 `llm_calls.jsonl` 复核，不能仅凭 Git 提交复核；这是明确的证据缺口，不伪造、不重建历史日志。
- a10-order / a10-line448 / a10-regression 是根目录未跟踪诊断文本；本轮保留原文件，未提交。其短摘录、路径和哈希已进证据摘要。

消费者检查：

- `acceptance.official_evaluation` 现在有 `official_evaluation_semantics`；旧字段仍为 `DEFERRED`，仅表示验收时官方评测被延后，不是最新 verdict。
- `official-evaluation-summary-l2cal10e.json`、`cost-ledger-reconciliation-l2cal10e.json`、`phase18e-summary.json` 只覆盖 a9 时点；历史文件不改写，M3 不沿用为最终汇总。
- 历史 a7 recheck 仍保留；新 official 入口不会把它当作无绑定通用绕过（schema 1 不满足 schema 2 哈希绑定）。

---

## 9. 保留项（未纳入本轮提交）

- 根目录 `a4-*`、`a9-*`、`a10-*` … `a21-*`、`final-*`、`wsl-hashes.txt` 等诊断文本：保留原文件。
- `.playwright-mcp/`、`logs/`、`pytest-of-lenovo/`、各 `tmp*` 临时目录、`.venv*`、缓存：保留，不提交。
- `artifacts/` 下未提交的 a12/a18 run report、patch_final、events、llm_calls 等：保留原文件；不把大日志或可能含敏感请求内容的文件提交。
- `artifacts/phase18/l2-cal10e/aggregate_l2cal10e.py`：保留原文件，不执行、不覆盖其 a9 历史输出。

---

## 10. 本轮状态声明

- **静态核实**：本文所有代码路径、JSON 字段、哈希比较、证据链均来自文件读取/JSON 解析/哈希计算；未导入项目模块执行项目逻辑。
- **已修改但未运行验证**：全部源码与测试改动；新增测试为“已编写、未执行”。
- **需要用户下一步授权**：运行 §6 测试、任何 Docker/Harness/模型调用、M3 正式批跑、M4/test50/test300。
- **保留结论**：pytest P2 = **NOT VERIFIED**；pytest 官方 = **NOT_EVALUATED / DEFERRED**；本轮未执行任何测试、构建、Docker、Harness 或模型调用。
