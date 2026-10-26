# Phase 22：原六实例真实恢复结果

日期：2026-10-26。唯一源码根：`C:\Users\lenovo\Desktop\CodePilot-Repair`；WSL 使用同一目录 `/mnt/c/Users/lenovo/Desktop/CodePilot-Repair`。所有金额为 CNY。

实际付费主尝试 **6/6**；可信终态 **6/6**；内部 ACCEPTED **1/6**；有效官方判分 **1/6**。批次状态 `completed`。这些是已见实例恢复验证，不代表未见泛化能力。

## 逐题结果

| 实例 | HTTP 次数 | 内部终态 | 官方 | 本轮新增费用 | Graph 秒 |
|---|---:|---|---|---:|---:|
| sphinx-doc__sphinx-8273 | 2 | SYSTEM_CONFIG_MISMATCH | NOT_EVALUATED | 0.02612600 | 未完整退出 |
| sympy__sympy-22714 | 26 | REJECTED | NOT_EVALUATED | 0.19821448 | 1463.080334 |
| matplotlib__matplotlib-18869 | 28 | REJECTED | NOT_EVALUATED | 0.12506384 | 1021.641406 |
| scikit-learn__scikit-learn-10297 | 18 | ACCEPTED | RESOLVED | 0.13205432 | 307.214198 |
| psf__requests-3362 | 4 | ENV_ERROR | NOT_EVALUATED | 0.04739080 | 168.800155 |
| django__django-11422 | 59 | REJECTED | NOT_EVALUATED | 0.39612992 | 1621.666894 |

每题最多一次付费主 Graph；Graph 内多次工具/模型请求不是第二次主尝试。执行顺序 Sphinx → SymPy → Matplotlib → sklearn → Requests → Django。所有原始记录保留；最终归一化结果及 SHA 见 `artifacts/phase22/results.json`。

- **Sphinx**：派发后发现 shared registry factory 忽略冻结的 timeout/trust_env。两次请求都已结算后停止进程，保留 SYSTEM_CONFIG_MISMATCH；修复后只继续未派发题。
- **SymPy**：MRO 索引性能问题在派发前修复；真实主 Graph 完成目标测试、完整回归和一次 Reviewer，但实际 Reviewer 输入遗漏完整补丁，旧 approved 不可信。新增 `review_context_bound` 严格验收后 REJECTED，未付费重审，未提交官方。
- **Matplotlib**：localizer 返回空文件集，最终没有授权目标测试，补丁从未完成目标闭环，REJECTED。真实 HTTP 已发生；原验收 real_run=false 表示缺少目标执行证明，不能解释为零模型。
- **sklearn**：目标、完整回归、独立 Reviewer 均通过；实际 Reviewer 请求包含完整候选补丁，validated/tested/reviewed/delivered/prediction 身份一致。官方有效 RESOLVED。
- **Requests**：复现 base workspace 的 packed tree 对象写入失败，内层 sandbox_error 被外层 NO_PATCH 掩盖；最终记 ENV_ERROR。修复 Git 原始对象读取、base-only copy 和停批判断，真实 Requests/Django base-copy 轻测均 PASS；未重跑 Requests。
- **Django**：旧未知费用原样继承；本轮独立 run/ledger，仅一次新主尝试。其实际 closure、失败原因、预算、测试次数和官方记录见下表后的逐题证据。

## 逐题可追溯证据

### sphinx-doc__sphinx-8273

- run_id：`swebench-smoke-phase22-six-live-sphinx-doc__sphinx-8273-s0-3cdd4e70`
- 报告：`None`；SHA256 `None`
- stop_reason：`None`；failure_reasons：`None`
- test_runs：`None`；effective budget：`null`
- closure：`null`
- 官方证据：`[]`

### sympy__sympy-22714

- run_id：`swebench-smoke-phase22-six-live-r2-sympy__sympy-22714-s0-e2006a67`
- 报告：`artifacts/phase22/live-r2-indexed/instances/sympy__sympy-22714/runs/swebench-smoke-phase22-six-live-r2-sympy__sympy-22714-s0-e2006a67/report.json`；SHA256 `6371a406ce595757deab9a29fb5ff061f3fccf90b0c313cbc4b51501cf7f3cdb`
- stop_reason：`None`；failure_reasons：`[]`
- test_runs：`9`；effective budget：`{"max_iterations": 5, "max_test_runs": 18, "max_files_changed": 5, "max_lines_changed": 150, "max_tool_calls_per_iteration": 30, "max_review_calls": 2, "max_runtime_seconds": 1800, "max_total_tokens": 1000000, "max_cost": 4.0, "budget_currency": "CNY", "max_cost_from_cli": 4.0, "cost_limit_enforced": true, "source": {"max_iterations": "cli", "max_test_runs": "cli", "max_tool_calls_per_iteration": "cli", "max_review_calls": "cli", "max_runtime_seconds": "cli:timeout"}, "config_hash": "e2006a6749419c2055c44b6d2c69ab69711a7d718c510806e64bd4f35402db45", "runner_overrides_sha256": "3a5f856f138a82b003403c77fffa60d47210979c4aabfc3d85dad4b649bba560"}`
- closure：`{"patch_present": true, "candidate_patch_sha256": "30fecf235bd0c33accba2c69b1d09ed0ec832e6951a06a679f5343fc8274bddc", "frozen_by_coding_stage": true, "coding_stop_reason": null, "validator_executed": true, "validator_passed": true, "target_tests_executed": true, "target_tests_status": "PASS", "target_test_sha256": "30fecf235bd0c33accba2c69b1d09ed0ec832e6951a06a679f5343fc8274bddc", "target_test_source": "planner", "target_tests": ["sympy/geometry/tests/test_point.py::test_Point2D", "sympy/parsing/tests/test_sympy_parser.py::test_function_evaluate_false", "sympy/parsing/tests/test_sympy_parser.py::test_recursive_evaluate_false_10560", "sympy/core/tests/test_expr.py::test_issue_22020"], "target_test_baseline_failures": [], "regression_executed": true, "regression_complete": true, "regression_mode": "full", "regression_levels": ["L1_target", "L2_impact", "L3_full"], "regression_new_failures": 0, "regression_fixed_tests": [], "regression_unexecuted_tests": 0, "review_executed": true, "review_approved": true, "review_high_issues": 0, "review_high_issues_known": true, "review_bound_to_candidate": true, "post_review_repair": false, "review_calls": 1, "unexecuted_stages": [], "closure_admission": {"feasible": true, "runs_spent": 6, "allowance": 18, "required": 3, "retry_reserve": 2, "target_runs": 1, "impact_runs": 1, "full_runs": 1, "coder_share": 7, "trigger": "generation", "detail": "test runs: 6 spent of 18, 12 left; closure requires 3 (patch validation needs 0 container run(s), target tests need 1, regression L2 needs 1, regression L3 needs 1) → a repair round can complete its closure (coding stage capped at 7 self-triggered run(s), 2 kept for flaky re-runs)"}, "budget_usage": {"iterations": "0/5", "test_runs": "9/18", "tool_calls": "4/30", "review_calls": "1/2", "tokens": "340885/1e+06", "cost": "0.198214/4", "time": "1463.08/1800"}, "cost_hard_cap": 4.0, "cost_hard_cap_hit": false}`
- 官方证据：`[]`

### matplotlib__matplotlib-18869

- run_id：`swebench-smoke-phase22-six-live-r3-matplotlib__matplotlib-18869-s0-759408fe`
- 报告：`artifacts/phase22/live-r3/instances/matplotlib__matplotlib-18869/runs/swebench-smoke-phase22-six-live-r3-matplotlib__matplotlib-18869-s0-759408fe/report.json`；SHA256 `08fb006dc19d29239acd9ca8e3ebf4ee64779de382ce5cd56eccdb8c47a70e22`
- stop_reason：`tests_failed`；failure_reasons：`['TARGET_TEST_UNAVAILABLE: no_authorized_target_test; the patch was never executed, so repair cannot be self-assessed']`
- test_runs：`8`；effective budget：`{"max_iterations": 5, "max_test_runs": 18, "max_files_changed": 5, "max_lines_changed": 150, "max_tool_calls_per_iteration": 30, "max_review_calls": 2, "max_runtime_seconds": 1800, "max_total_tokens": 1000000, "max_cost": 4.06550088, "budget_currency": "CNY", "max_cost_from_cli": 4.06550088, "cost_limit_enforced": true, "source": {"max_iterations": "cli", "max_test_runs": "cli", "max_tool_calls_per_iteration": "cli", "max_review_calls": "cli", "max_runtime_seconds": "cli:timeout"}, "config_hash": "759408fe5659e2243b1ee6e568f9aa0e60dd006cdbe3620af4e33d66b6e27e01", "runner_overrides_sha256": "3a5f856f138a82b003403c77fffa60d47210979c4aabfc3d85dad4b649bba560"}`
- closure：`{"patch_present": true, "candidate_patch_sha256": "ca33eff72a9fe11f313f970b7fff0996afb4300402ff20f18fda12f538efad61", "frozen_by_coding_stage": true, "coding_stop_reason": null, "validator_executed": true, "validator_passed": true, "target_tests_executed": false, "target_tests_status": null, "target_test_sha256": "ca33eff72a9fe11f313f970b7fff0996afb4300402ff20f18fda12f538efad61", "target_test_source": "none", "target_tests": [], "target_test_baseline_failures": [], "regression_executed": false, "regression_complete": false, "regression_mode": null, "regression_levels": [], "regression_new_failures": 0, "regression_fixed_tests": [], "regression_unexecuted_tests": 0, "review_executed": false, "review_approved": null, "review_high_issues": 0, "review_high_issues_known": false, "review_bound_to_candidate": false, "post_review_repair": false, "review_calls": 0, "unexecuted_stages": ["target_tests", "regression", "review:the reviewer produced no result"], "closure_admission": {"feasible": true, "runs_spent": 6, "allowance": 18, "required": 1, "retry_reserve": 2, "target_runs": 1, "impact_runs": 0, "full_runs": 0, "coder_share": 9, "trigger": "generation", "detail": "test runs: 6 spent of 18, 12 left; closure requires 1 (patch validation needs 0 container run(s), target tests need 1, regression L2 needs 0, regression L3 needs 0) → a repair round can complete its closure (coding stage capped at 9 self-triggered run(s), 2 kept for flaky re-runs)"}, "budget_usage": {"iterations": "0/5", "test_runs": "8/18", "tool_calls": "11/30", "review_calls": "0/2", "tokens": "229708/1e+06", "cost": "0.125064/4.0655", "time": "1021.64/1800"}, "cost_hard_cap": 4.06550088, "cost_hard_cap_hit": false}`
- 官方证据：`[]`

### scikit-learn__scikit-learn-10297

- run_id：`swebench-smoke-phase22-six-live-r3-scikit-learn__scikit-learn-10297-s0-f2cbe85f`
- 报告：`artifacts/phase22/live-r3/instances/scikit-learn__scikit-learn-10297/runs/swebench-smoke-phase22-six-live-r3-scikit-learn__scikit-learn-10297-s0-f2cbe85f/report.json`；SHA256 `d5b78352c8471fa144ac7ac5a40c014c9503edc5f717ca3d4f8677ef7e631e60`
- stop_reason：`None`；failure_reasons：`[]`
- test_runs：`9`；effective budget：`{"max_iterations": 5, "max_test_runs": 18, "max_files_changed": 5, "max_lines_changed": 150, "max_tool_calls_per_iteration": 30, "max_review_calls": 2, "max_runtime_seconds": 1800, "max_total_tokens": 1000000, "max_cost": 4.0, "budget_currency": "CNY", "max_cost_from_cli": 4.0, "cost_limit_enforced": true, "source": {"max_iterations": "cli", "max_test_runs": "cli", "max_tool_calls_per_iteration": "cli", "max_review_calls": "cli", "max_runtime_seconds": "cli:timeout"}, "config_hash": "f2cbe85ff040b5a29a59378b333d3765e882a58eebcc5318814af29ddd739555", "runner_overrides_sha256": "3a5f856f138a82b003403c77fffa60d47210979c4aabfc3d85dad4b649bba560"}`
- closure：`{"patch_present": true, "candidate_patch_sha256": "b383d3f60ecc9896813089bc27d9747af8d30e137793bab78f3dc26a6d286d59", "frozen_by_coding_stage": true, "coding_stop_reason": null, "validator_executed": true, "validator_passed": true, "target_tests_executed": true, "target_tests_status": "PASS", "target_test_sha256": "b383d3f60ecc9896813089bc27d9747af8d30e137793bab78f3dc26a6d286d59", "target_test_source": "planner", "target_tests": ["sklearn/linear_model/tests/test_ridge.py::test_ridgecv_store_cv_values", "sklearn/linear_model/tests/test_ridge.py::test_ridge_cv_sparse_svd", "sklearn/linear_model/tests/test_ridge.py::test_class_weights_cv", "sklearn/linear_model/tests/test_ridge.py::test_ridgecv_sample_weight", "sklearn/linear_model/tests/test_ridge.py::test_class_weight_vs_sample_weight"], "target_test_baseline_failures": [], "regression_executed": true, "regression_complete": true, "regression_mode": "full", "regression_levels": ["L1_target", "L2_impact", "L3_full"], "regression_new_failures": 0, "regression_fixed_tests": [], "regression_unexecuted_tests": 0, "review_executed": true, "review_approved": true, "review_high_issues": 0, "review_high_issues_known": true, "review_bound_to_candidate": true, "post_review_repair": false, "review_calls": 1, "unexecuted_stages": [], "closure_admission": {"feasible": true, "runs_spent": 6, "allowance": 18, "required": 3, "retry_reserve": 2, "target_runs": 1, "impact_runs": 1, "full_runs": 1, "coder_share": 7, "trigger": "generation", "detail": "test runs: 6 spent of 18, 12 left; closure requires 3 (patch validation needs 0 container run(s), target tests need 1, regression L2 needs 1, regression L3 needs 1) → a repair round can complete its closure (coding stage capped at 7 self-triggered run(s), 2 kept for flaky re-runs)"}, "budget_usage": {"iterations": "0/5", "test_runs": "9/18", "tool_calls": "5/30", "review_calls": "1/2", "tokens": "190569/1e+06", "cost": "0.132054/4", "time": "307.214/1800"}, "cost_hard_cap": 4.0, "cost_hard_cap_hit": false}`
- 官方证据：`[{"path": "artifacts/phase22/live-r3/instances/scikit-learn__scikit-learn-10297/official-evaluation-phase22-six-live-r3.json", "sha256": "50a8a02a03a57f48999d253b264e5df709a463f2f8f2121311ab6abc8ed6a9e7", "verdict": "RESOLVED", "counts_toward_resolved_rate": true, "report_sha256": "ca9a879e9982f7910bdcdf661669bfd54db86cff29e23d1b7441da1e0b9987c3"}]`

### psf__requests-3362

- run_id：`swebench-smoke-phase22-six-live-r4-psf__requests-3362-s0-d24f57e7`
- 报告：`artifacts/phase22/live-r4/instances/psf__requests-3362/runs/swebench-smoke-phase22-six-live-r4-psf__requests-3362-s0-d24f57e7/report.json`；SHA256 `ee5856cf77df74ee77e3763fcf76a4f2a79ce19b247be999d702f3ec097e8a02`
- stop_reason：`sandbox_failure`；failure_reasons：`['reproduce environment failure: workspace_error | workspace_error | base Workspace 创建失败: 写入 workspace 对象失败: 5602c48ac5ca0cabc7d13e5fddc339410dc7af18 | declared=AssertionError: iter_content(decode_unicode=True) yielded b\'{"status": "ok"}\' of type <class \'bytes\'>, expected unicode/str — on the base commit stream_decode_response_unicode returns the byte iterator unchanged (it bails out early), so iter_content(decode_unicode=True) yields bytes instead of str; after the fix the chunks are decoded unicode and the test passes.']`
- test_runs：`2`；effective budget：`{"max_iterations": 5, "max_test_runs": 18, "max_files_changed": 5, "max_lines_changed": 150, "max_tool_calls_per_iteration": 30, "max_review_calls": 2, "max_runtime_seconds": 1800, "max_total_tokens": 1000000, "max_cost": 4.0, "budget_currency": "CNY", "max_cost_from_cli": 4.0, "cost_limit_enforced": true, "source": {"max_iterations": "cli", "max_test_runs": "cli", "max_tool_calls_per_iteration": "cli", "max_review_calls": "cli", "max_runtime_seconds": "cli:timeout"}, "config_hash": "d24f57e75307aff57de2adeb7af40627137081d3f7d3e8f65f74bc1496750694", "runner_overrides_sha256": "3a5f856f138a82b003403c77fffa60d47210979c4aabfc3d85dad4b649bba560"}`
- closure：`{"patch_present": false, "candidate_patch_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "frozen_by_coding_stage": false, "coding_stop_reason": null, "validator_executed": false, "validator_passed": null, "target_tests_executed": false, "target_tests_status": null, "target_test_sha256": "", "target_test_source": null, "target_tests": [], "target_test_baseline_failures": [], "regression_executed": false, "regression_complete": false, "regression_mode": null, "regression_levels": [], "regression_new_failures": 0, "regression_fixed_tests": [], "regression_unexecuted_tests": 0, "review_executed": false, "review_approved": null, "review_high_issues": 0, "review_high_issues_known": false, "review_bound_to_candidate": false, "post_review_repair": false, "review_calls": 0, "unexecuted_stages": ["patch_validation", "target_tests", "regression", "review:the reviewer produced no result"], "closure_admission": null, "budget_usage": {"iterations": "0/5", "test_runs": "2/18", "tool_calls": "0/30", "review_calls": "0/2", "tokens": "54306/1e+06", "cost": "0.0473908/4", "time": "168.8/1800"}, "cost_hard_cap": 4.0, "cost_hard_cap_hit": false}`
- 官方证据：`[]`

### django__django-11422

- run_id：`swebench-smoke-phase22-six-live-r5-django__django-11422-s0-f137c0d5`
- 报告：`artifacts/phase22/live-r5/instances/django__django-11422/runs/swebench-smoke-phase22-six-live-r5-django__django-11422-s0-f137c0d5/report.json`；SHA256 `5416475dc6ff305f10860036cfe90e812ce05b80f84f561e2048a8e187104ce3`
- stop_reason：`budget_time`；failure_reasons：`['existing test file modification is not allowed: tests/utils_tests/test_autoreload.py', 'existing test file modification is not allowed: tests/utils_tests/test_autoreload.py', 'existing test file modification is not allowed: tests/utils_tests/test_autoreload.py', 'existing test file modification is not allowed: tests/utils_tests/test_autoreload.py', 'existing test file modification is not allowed: tests/utils_tests/test_autoreload.py', 'existing test file modification is not allowed: tests/utils_tests/test_autoreload.py', "repair_patch: budget exhausted (budget_time); usage={'iterations': '1/5', 'test_runs': '10/18', 'tool_calls': '11/30', 'review_calls': '0/2', 'tokens': '665906/943808', 'cost': '0.39613/4.79772', 'time': '1619.71/1560', 'reserved': 'tokens=56192,cost=0.0613376,time=240'}; reserved for the verification/review closure: tokens=56192, cost=0.0613376, time=240 (estimated=True, basis=reviewer-model:glm-5.3-flash-main)"]`
- test_runs：`10`；effective budget：`{"max_iterations": 5, "max_test_runs": 18, "max_files_changed": 5, "max_lines_changed": 150, "max_tool_calls_per_iteration": 30, "max_review_calls": 2, "max_runtime_seconds": 1800, "max_total_tokens": 1000000, "max_cost": 4.8590564, "budget_currency": "CNY", "max_cost_from_cli": 4.8590564, "cost_limit_enforced": true, "source": {"max_iterations": "cli", "max_test_runs": "cli", "max_tool_calls_per_iteration": "cli", "max_review_calls": "cli", "max_runtime_seconds": "cli:timeout"}, "config_hash": "f137c0d5fe5b70e40a07667d9fcca651a5a2af9d6ee262b163f6f1f718c49cec", "runner_overrides_sha256": "3a5f856f138a82b003403c77fffa60d47210979c4aabfc3d85dad4b649bba560"}`
- closure：`{"patch_present": true, "candidate_patch_sha256": "56e73b61193e41113681adac71d0c424393817ff2a848c71b2ec6ca7e2ce55c9", "frozen_by_coding_stage": true, "coding_stop_reason": null, "validator_executed": true, "validator_passed": false, "target_tests_executed": false, "target_tests_status": null, "target_test_sha256": "56e73b61193e41113681adac71d0c424393817ff2a848c71b2ec6ca7e2ce55c9", "target_test_source": null, "target_tests": [], "target_test_baseline_failures": [], "regression_executed": false, "regression_complete": false, "regression_mode": null, "regression_levels": [], "regression_new_failures": 0, "regression_fixed_tests": [], "regression_unexecuted_tests": 0, "review_executed": false, "review_approved": null, "review_high_issues": 0, "review_high_issues_known": false, "review_bound_to_candidate": false, "post_review_repair": false, "review_calls": 0, "unexecuted_stages": ["patch_validation", "target_tests", "regression", "review:the reviewer produced no result"], "closure_admission": {"feasible": true, "runs_spent": 9, "allowance": 18, "required": 3, "retry_reserve": 2, "target_runs": 1, "impact_runs": 1, "full_runs": 1, "coder_share": 4, "trigger": "validation", "detail": "test runs: 9 spent of 18, 9 left; closure requires 3 (patch validation needs 0 container run(s), target tests need 1, regression L2 needs 1, regression L3 needs 1) → a repair round can complete its closure (coding stage capped at 4 self-triggered run(s), 2 kept for flaky re-runs)"}, "budget_usage": {"iterations": "1/5", "test_runs": "10/18", "tool_calls": "11/30", "review_calls": "0/2", "tokens": "665906/1e+06", "cost": "0.39613/4.85906", "time": "1621.67/1800"}, "cost_hard_cap": 4.8590564, "cost_hard_cap_hit": false}`
- 官方证据：`[]`

## Provider 调查及冻结配置

Phase21 Django 第五次失败是 **31.921 秒 RemoteProtocolError**，不是已证实的 120 秒 ReadTimeout。旧客户端四类 timeout 均为 120 秒；第五次 input usage 未知，不能写 0。旧请求体未保存精确 wire bytes、响应头、服务端 request_id 和完整异常链，无法事后重建。旧已知输入 token 为 14775/24554/5132/6263，输出上限 4096。代理是候选原因；服务端、网络和进程原因没有足够证据定论。休眠事件检查未发现临近电源事件，但这不证明网络正常。

本轮冻结 alias `glm-5.3-flash-main`，实际 request model `glm-5.3-flash`，temperature=0，HTTP/SDK retries=0。connect/read/write/pool = **10/120/30/10 秒**，`trust_env=false`；Graph 1800 秒，Reviewer 预留 240 秒。没有盲目延长 read，没有切换模型，真实诊断请求 **0**；仅无费用 TLS 检查（TLS1.3，0.422 秒）。Sphinx 首段实际 factory 仍用了旧默认，不能以期望配置冒充实际配置；9f8fc16 后真实客户端 timeout/trust_env 已记录验证。

每次派发前持久化预扣、请求 SHA、purpose、角色、请求体大小估计、客户端实际 timeout/trust_env、耗时、异常类型链/timeout 阶段、可用响应 request_id/status。`request_body_bytes` 是 JSON 序列化估计，不是经测量的精确 HTTP wire bytes。不得记录凭据。Mock 覆盖读超时、协议错误、取消/中断后冻结和停止。新请求未知立即冻结，不能自动开新账本绕过。

## 系统修复与执行能力

六题镜像/runner/scope 身份及轻量真实测试均通过，见 `environment-summary.json`；不把零模型轻测称为完整 Agent 成功。索引仅在 SQLite integrity、repo/base/source/file hash 完全一致时复用。SymPy 通过 immutable resolver 缓存 name/bases/MRO，真实完整 1944 文件索引 694.063 秒；修复前 profile 的 996 样本中 541 个含 MRO，修复后为 0。

Reviewer 从单个超大 facts 改为紧凑事实、必要源码片段与不可截断的完整补丁；请求必须实际包含完整补丁和计划事实，否则在派发前失败。验收再次验证实际保留请求和候选 SHA；SymPy 历史假通过已明确撤销。普通 RunReport 的 effective budget 从 run 身份绑定的外层报告读取，max_test_runs=18。

packed tree 缺陷的实际对象 `5602c48ac5ca0cabc7d13e5fddc339410dc7af18`：`cat-file -p` 产生人类可读 tree，重新 hash-object 失败；改为按真实类型读取原始 bytes 并校验对象 SHA。benchmark base provider 使用已验证的一提交干净缓存 copy。内层 sandbox_error/环境错误现在强制 ENV_ERROR 并停批，不能被 NO_PATCH 外层吞掉。

批次停机对所有未派发题写 NOT_DISPATCHED，对已派发题写真实停止状态；区分空账本中断和付费中断。新增进程锁、PID/退出码/日志、已有 eval 拒绝覆盖、manifest/config/code/budget 身份校验。后台 detached 会话丢失发生在派发前，随后使用保留的 foreground supervisor。所有有费用的系统停止只修复后续未派发题，不重跑已付费题。

Windows Python 无法启动 WSL（E_ACCESSDENIED）；sklearn 首次启动失败没有启动 Harness、没有伪造 UNRESOLVED。随后通过工具 PowerShell 在同一 WSL 项目目录进行一次实际 Harness，得到有效 RESOLVED。官方缺记录、启动错误、ERROR/INVALID_ENV 均保留。外部 ingest 只读取已生成结果，不再付费派发。

历史 Phase21 原件不改；`phase21-final-supplement.json` 明确 Django DISPATCHED_STOPPED/PROVIDER_USAGE_UNCERTAIN，后五题 NOT_DISPATCHED/BLOCKED_BY_FROZEN_LEDGER，官方均 NOT_EVALUATED。

## 费用对账

历史 opening 11.24078560 = 已结算 7.18344160 + 历史未知预留 4.05734400。原 30 元授权从 Phase20 计算，不把上述历史 opening 再计入新增授权。Phase20 新增 0.25156496；Phase21 新增已结算 0.04758760 + Django 未知预留 0.81146880；Phase20+21 上界 **1.11062136**，来自实际 ledger hash 复算。

本轮新增已结算 **0.92497936**，新增未知预留 **0.00000000**；原 30 元授权累计上界 **2.03560072**，余额 **27.96439928**。本轮 24 元上限余额 **23.07502064**。包含历史的总上界 **13.27638632**。各题新增均低于 4 元；没有真实诊断费用。

旧 Django frozen ledger SHA256 `348d46f251f73f22cc207fda8374ca49b40a85d453199d2a6a1318daa439a046`，旧字节原样保留；0.8114688 只是继续预留，不声称服务商已结算。每段 manifest 引用前段 SHA，opening 只继承一次；本轮金额只累计各请求 cost，不重复累加 ledger opening。详见 `opening-audit.json`、`results.json`、各冻结 opening/config 和 `final-audit.json`。

## 指标与准入

官方覆盖率 16.67%；有效官方解决率 100.00%；任务级解决率 16.67%；内部闭环通过率 16.67%。本轮 Provider 未知停止率 0.00%；另有 Sphinx 配置缺陷停止 1/6。环境/工作区失败率 Requests 1/6=16.67%，镜像轻测失效率 0/6。系统缺陷造成的停止不计为任务解决。

下一阶段准备门禁：**通过**。运行时观察到的系统问题已修复并有针对性验证；不以已付费题重复执行证明修复。

已准备 `artifacts/phase22/next-stage/`：50 题未见候选、保守排除集及来源、固定 seed=20261026 的 SHA256 抽样、逐题只读镜像与 runner 覆盖审计、费用估算和运行规格。样本不按结果挑选，不读取隐藏 patch/test_patch/FAIL_TO_PASS/PASS_TO_PASS/hints。未缓存镜像和未核验 runner 明确列出，不能把准备完成称为 50 题环境全部可用。

固定清单有 50 个唯一候选，排除表 17 个历史 ID（含不在当前 split 内的历史 ID），当前 split 排除后 284 个可抽样项。只读 Docker 审计显示 50/50 预期官方镜像未缓存，50/50 的 runner/scope/dependencies 均须逐题预检。

test50 状态 PREPARATION_ONLY；没有启动任何 test50/test300/M4。拟议硬上限 50×4=200 元只是后续规格，需要未来明确预算及派发授权；当前原 30 元余额没有自动拨给 test50。六题均值外推仅为偏低的小样本估算，不是费用保证。

## 验证与本地提交

受影响门禁日志均保留于 artifacts/phase22；以下分组有重叠，不能相加当独立测试数：

| 验证组 | 结果 |
|---|---|
| initial-gates（Provider/预算/closure/registry 等） | 214 passed |
| Provider 独立组 | 56 passed |
| native runner/M3/acceptance/prediction | 90 passed |
| dispatch timeout/异常/取消/实际 client factory | 15 passed |
| snapshot identity/content/hash | 4 passed |
| MRO/name resolution | 69 passed |
| Reviewer 完整输入及 budget/closure | 95 passed |
| official missing/invalid handoff | 27 passed |
| packed workspace 与 inner sandbox stop | 51 passed |
| workspace 全相关组 | 91 passed，初始 2 Docker skipped |
| 上述 2 个真实 Docker 测试单独执行 | 2 passed，9 deselected |
| 真实 Requests/Django base-copy native probe | 2 PASS |
| mypy | 198 source files success |
| 相关 Ruff lint/format | passed |

没有削弱已有测试断言、删除目标或降低验收标准。相关实现用新增反例阻止假通过。最终额外门禁 `final-hardening-tests.log` 为 30 passed；最终 mypy 再次检查 198 文件通过，Ruff check/format 通过。

| 提交 | 内容 |
|---|---|
| `0ad26ca` | fix: preserve provider uncertainty and finalize stopped recovery batches |
| `6b46e0a` | chore: freeze Phase22 six-instance recovery budget and direct provider route |
| `9f8fc16` | fix: propagate frozen network settings through shared client factory |
| `2b843ff` | chore: freeze repaired segment for five undispatched Phase22 tasks |
| `45ae6c7` | fix: retain recovery supervisor in the active execution session |
| `d5fa740` | perf: cache immutable name and MRO resolution for SymPy indexing |
| `c70138f` | fix: require complete reviewer input and read normal run budget evidence |
| `fb4630c` | chore: freeze four-task continuation without rerunning paid instances |
| `bf274ed` | fix: preserve official launch errors and support supervised handoff continuations |
| `b67e37f` | chore: freeze final two tasks and external official handoff |
| `19895b7` | fix: preserve packed Git object bytes and stop on inner sandbox failures |
| `f329f3b` | chore: freeze Django-only continuation after base workspace recovery |
| `3316c78` | fix: preserve terminal batch stops across restarts |

冻结运行代码依次为首段 6b46e0a、SymPy d5fa740、Matplotlib/sklearn fb4630c、Requests b67e37f、Django f329f3b。最终文档与准备提交发生在运行结束后，不冒充运行时版本。只本地提交；没有 push、PR、历史删除或重写。
