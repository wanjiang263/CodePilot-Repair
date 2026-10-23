# Phase 18：内部 M2 环境恢复与 L2-Cal10 费用校准

日期：2026-10-24（初稿 2026-10-22）。状态：**任务 A 已验证；原 Cal10 在 `pydata__xarray-4493` 的 `ReadTimeout` 处 fail-closed 停批；l2cal10e 追加尝试至 `l2cal10e-a21` 后停止，pytest P2 仍为 `NOT VERIFIED`，官方 Harness 未运行。**

> 阅读顺序：第 1–2 节是 **原 Cal10 历史快照**（只覆盖 l2cal10c 的 7/10 与 xarray 停止点）；第 3–5 节保留当时的风险与 Git 记录；**最新 l2cal10e/a10–a21 结论以第 6 节为准**。第 6 节之前关于「pylint / pytest / scikit-learn 未执行」的表述是 2026-10-22 的时点记录，不是当前状态。

本记录严格区分「代码实现」「测试收集」「实际测试执行」「官方判分」「费用结算」五种状态，不把任何一个当作另一个。

---

## 1. 任务 A：内部 M2 环境恢复（VERIFIED）

### 1.1 根因

Phase 16 的 `COLLECTION_ERROR` 不是复现器或断言逻辑的问题，而是执行镜像环境：

- `pvlib__pvlib-python-1072` 官方镜像内 NumPy 2.0.2 与 PyTables 3.9.2（cp39，按 NumPy 1.x ABI 构建）不兼容；
- conftest 导入 `tables` 时即失败，pytest 在收集前退出（exit 4）；
- 内部 `CachedOfficialSandbox` 只使用官方 `:latest` 标签，无法选择 Phase 17 已验证的修复镜像，也没有镜像 ID 校验或失败闭合。

### 1.2 生产修复

| 变更 | 内容 |
|---|---|
| `src/codepilot/sandbox/swebench_env.py` | 新增 `SWEBenchInstanceImageResolver` / `InstanceImageOverride` / `ResolvedInstanceImage`；按实例解析镜像，校验完整 `sha256` ID；override 缺失或 ID 不匹配时 fail closed，不回退官方标签 |
| `src/codepilot/sandbox/official.py` | 新增生产 `OfficialInstanceSandbox(DockerSandbox)`：绑定实例镜像、校验 workspace HEAD/base commit、镜像 OS/架构/tag/ID，并在 `/testbed` 执行 pytest |
| `configs/swebench/instance-image-overrides.json` | 仅登记 `pvlib__pvlib-python-1072` 的 Phase 17 修复镜像（父镜像 digest、完整镜像 ID、原因、证据） |
| `scripts/swebench_smoke.py` | `--image-overrides`；环境预检、ensure、sandbox 全部使用同一解析结果；记录实际执行镜像与 override 来源 |
| `tests/unit/sandbox/test_instance_image_resolution.py` | 覆盖默认官方镜像、单实例作用域、父镜像错误、ID 不匹配、镜像缺失禁止回退，以及 `COLLECTION_ERROR != FAIL != ENV_ERROR` |

未修改 SWE-bench 原始 Issue、base commit、隐藏测试、官方评分逻辑或 Phase 17 修复镜像。

Task A 是生产组件级恢复：`run_recovery.py` 使用 `OfficialInstanceSandbox`、生产 JUnit/status 解析和 `execution_route`，没有运行 `InferenceAssembly`/完整 LangGraph；其“Graph 回传”仅指状态与路由函数结果。

### 1.3 真实 Docker 证据（零模型）

run_id：`phase18-m2-recovery-20261022T044506Z`。

| 检查 | 结果 |
|---|---|
| 输入补丁 SHA-256 | `ab393289bf1dc1da0a5c0d7b7f0153d656081d581898ba755b363c30068643a7`，与授权输入一致 |
| 输入预测 SHA-256 | `35b4a6fe82835e834ad176d501993f4a79b488854d93b0d5b5fa6f0e4f905dfd`，与授权输入一致 |
| 工作区 | base `04a523fafbd61bc2e49420963b84ed8e2bd1b3cf`，干净，恰好 1 个可达提交 |
| 实际镜像 | `phase17/sweb.eval.x86_64.pvlib_1776_pvlib-python-1072:phase17-numpy1264`，ID `sha256:e80d07e55c35282ad03d085a9476376586bddb287c239bea1d69f4d92fe31a82`；父镜像 `sha256:c8e43bebd10d7d2330820af520361dc6adc9642f98c7b7b7c415cca39852ffdd` |
| 补丁应用 | 生产 `apply_unified_diff` 成功，3 文件；应用后 `git diff` SHA-256 = 输入补丁 SHA-256 |
| 复现测试收集 | `collected 1 item`，exit 0，JUnit XML 生成 |
| 未打补丁复现测试 | 真实执行，结果 `FAIL`（`TypeError: float() argument ... 'Timedelta'`） |
| 未打补丁目标测试 | `pvlib/tests/test_temperature.py::test_fuentes`，`PASS`（2 cases） |
| 打补丁复现测试 | `PASS` |
| 打补丁目标测试 | `PASS`（2 cases） |
| 内部回归 | `pvlib/tests/test_temperature.py` 全文件 17 passed |
| JUnit/退出码 | 生产解析器读取成功；Graph 路由 `target_tests → regression`，`status=succeeded` |
| 清理 | 新增 CodePilot 容器 0、临时卷 0 |
| 模型调用 / 费用 | **0 / CNY 0.00** |

证据：`artifacts/phase18/m2-environment-recovery/evidence/manifest.json`（`checks` 全部为 true）及同目录的 collection/base/patched/JUnit 原始输出；运行现场在 WSL `artifacts/phase18/m2-environment-recovery/runs/phase18-m2-recovery-20261022T044506Z/`。

### 1.4 定向测试

WSL2 Ubuntu-24.04、项目 `.venv`：

```text
tests/unit/sandbox/test_instance_image_resolution.py
tests/unit/sandbox/test_status.py
tests/sandbox/test_swebench_env.py
90 passed in 3.32s
```

未运行全量 pytest、完整 Docker 测试集或前端测试。任务 A 未使用 Phase 17 官方 Harness 的 19 项结果作为替代证据。

---

## 2. 任务 B：L2-Cal10（PARTIAL）

### 2.1 冻结基线与预检

- 清单：`configs/subsets/swebench-lite-cal10.json`，SHA-256 `0a1119db38d578f17ff4f45695a5412b8e2a78459650461e9cdd538aa187a135`。
- 实例列表 SHA-256：`1e8fb7a42078a23757183ae7ceb80ffb495545a3a193ea0d975ccf97156f5c3e`。
- 数据集 revision：`6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2`，split `test`。
- 模型：provider `bigmodel`，别名 `glm-5.3-flash-main` → 实际模型 `glm-5.3-flash`；未换模。
- `preflight.txt` 是更早一次探测，记录 10 个 `IMG_MISSING`；真正生效的是 `run_batch_v2.sh:50-55` 的内联 inspect 门禁，`batch-status-l2cal10c.log` 没有 `PREFLIGHT_FAIL` 且继续 START，可证明该门禁通过。应归档本次成功 inspect 的完整输出。
- 每实例一次性单提交 bare repo-cache 已预置（`prepare_cal10_caches.py`），并由生产 `WorkspaceManager` 实测 materialization：1 个可达提交、HEAD=base、干净。
- 预算门禁：每题派发前读取全部已结算账本，冻结/未结算即停；单题 `min(3.00, 整批剩余)`，整批上限 30.00。

### 2.2 停止位置

- 停止实例：`pydata__xarray-4493`。
- 停止原因：第 3 次模型调用发生 `ReadTimeout`；`DispatchBudget` 将该请求记为 `unknown_frozen`，费用未知，账本按 fail-closed 冻结。
- 批次脚本在下一题 `pylint-dev__pylint-6506` 派发前检测到冻结账本，**没有派发任何后续请求**。
- `pylint-dev__pylint-6506`、`pytest-dev__pytest-11148`、`scikit-learn__scikit-learn-12471` 未执行。
- 没有自动重试、没有重置/拆分账本、没有切换模型、没有复用 Phase 16/17 的预测或补丁。

### 2.3 十题结果

| # | 实例 | 补丁 SHA-256 | 实际执行镜像 | 内部 M2 | 官方 verdict | 环境有效 | 调用 | Token in/out | 费用 CNY | 未执行阶段 |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | astropy__astropy-12907 | `b86e615b347d95438c2f23d4f8d83d14c6e0cd8f6f3fce1772fd14ef592e036e` | `swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest` / `sha256:e082963099ed…` | failed/tests_failed | RESOLVED | 是 | 20 | 159739/6810 | 0.142409 | — |
| 2 | django__django-11910 | `a4aae59af1bade8240604942630153de6c931bab072afe66a418dcf4279aa3ce` | `swebench/sweb.eval.x86_64.django_1776_django-11910:latest` / `sha256:1b7d3771528b…` | failed/tests_failed | UNRESOLVED | 是 | 24 | 221834/16222 | 0.187175 | — |
| 3 | matplotlib__matplotlib-23314 | —（无补丁） | `swebench/sweb.eval.x86_64.matplotlib_1776_matplotlib-23314:latest` / `sha256:f88a00b6cfc8…` | plan_rejected | NOT_EVALUATED | 是 | 17 | 148472/7386 | 0.121948 | INTERNAL_M2_GRAPH |
| 4 | mwaskom__seaborn-3010 | `e9f97d2792f5587ccf2457c4936fad34bd40b3410a31b53f893c46aa596264ad` | `swebench/sweb.eval.x86_64.mwaskom_1776_seaborn-3010:latest` / `sha256:22fef36e8b96…` | failed/tests_failed（内部闭环完成） | RESOLVED | 是 | 19 | 168080/5117 | 0.134236 | — |
| 5 | pallets__flask-4045 | `de4f5bcfd30d7600761d60935e521b3b45afa43663d1bc8b47c36dedd326cc79` | `swebench/sweb.eval.x86_64.pallets_1776_flask-4045:latest` / `sha256:1425b0801ddf…` | succeeded（内部闭环完成） | UNRESOLVED | 是 | 31 | 354314/11828 | 0.242150 | — |
| 6 | psf__requests-2148 | `c191f732f02dee5a35692cca5f57b09e75e806023f15d8129cbc117413aefd93` | `swebench/sweb.eval.x86_64.psf_1776_requests-2148:latest` / `sha256:ada704cd7cd3…` | stopped/diagnosis_give_up（内部闭环完成） | UNRESOLVED | 是 | 28 | 364409/6732 | 0.209035 | — |
| 7 | pydata__xarray-4493 | —（无补丁） | `swebench/sweb.eval.x86_64.pydata_1776_xarray-4493:latest` / `sha256:5c94544db403…` | error/workflow_error | NOT_EVALUATED | 是 | 3 | 24527/123 | 0.831435（含未知预留） | INTERNAL_M2_GRAPH |
| 8 | pylint-dev__pylint-6506 | — | — | NOT_EXECUTED | NOT_EVALUATED | 否 | 0 | 0/0 | 0.000000 | PRE_MODEL_DISPATCH |
| 9 | pytest-dev__pytest-11148 | — | — | NOT_EXECUTED | NOT_EVALUATED | 否 | 0 | 0/0 | 0.000000 | PRE_MODEL_DISPATCH |
| 10 | scikit-learn__scikit-learn-12471 | — | — | NOT_EXECUTED | NOT_EVALUATED | 否 | 0 | 0/0 | 0.000000 | PRE_MODEL_DISPATCH |

- 完整补丁哈希、完整镜像 ID、per-run report 路径见 `artifacts/phase18/l2-cal10/cal10-results.json`。
- 官方判分只认 Harness verdict；内部 M2 终态不参与官方判定。
- `matplotlib`：模型连续 5 次生成未通过语义/权限校验的 RepairPlan，无合法补丁，未进入官方 Harness。
- `xarray`：`ReadTimeout` 后账本冻结；`unknown_frozen` 预留 `0.8114688` CNY 保留在账本中，不按零计费。该题不是环境无效，也不是模型修复失败，而是**费用未知的共享 Provider 故障**。xarray 的 `cost=null` 出现在 `runs/.../llm_requests.jsonl`；同目录 `llm_calls.jsonl` 的最后一条是请求记录，本身不写 usage。
- `astropy`、`seaborn` 的官方结果虽为 RESOLVED，但内部 M2 没有声称成功（`astropy` 卡在无法授权的目标测试；`seaborn` 内部回归不完整）；**不得把官方 RESOLVED 反推为内部 M2 通过**。
- `flask` 内部 M2 `succeeded`，官方仍为 UNRESOLVED——同样不能把内部状态当作官方结论。

### 2.4 率与费用

| 指标 | 计算 | 结果 |
|---|---|---|
| 有效官方评测分母 | 环境有效 + 官方 Harness 真实完成 | **5**（astropy、django、seaborn、flask、requests） |
| 有效官方 RESOLVED | 上述 5 题中的 RESOLVED | **2**（astropy、seaborn） |
| 有效官方解决率 | 2 ÷ 5 | **40.0%** |
| 内部链路形成终态率 | 内部测试实际执行、形成内部终态、未卡在“无授权目标测试” | **3/10 = 30.0%**（seaborn、flask、requests）；其中 seaborn 为 `failed/tests_failed`、requests 为 `stopped/diagnosis_give_up`，不等于验证通过 |
| 内部 M2 声称成功 | `agent_claims_success=true` | **1/10**（flask） |
| 已结算费用 | 所有 settled 记录 `cost_cny` 之和 | **CNY 1.056919** |
| 未知费用预留 | xarray 的 `unknown_frozen` 预留（上界） | **CNY 0.811469** |
| 记账总费用上界 | 已结算 + 未知预留 | **CNY 1.868388** |
| 单题最大记账 | xarray | **CNY 0.831435 ≤ 3.00** |
| 整批上限 | — | **CNY 1.868388 ≤ 30.00** |
| 剩余批次预算（按上界） | 30.00 − 1.868388 | **CNY 28.131612** |
| 账本对账 | 141 次按服务商 Usage 实际结算 + 1 次按预留上界入账 | **完成（保守上界口径）** |
| 未结算/冻结 | xarray | **1 个 ReadTimeout；实际用量不可见，按预留上界入账；账本仍冻结，后续未派发** |

费用对账按保守上界完成：`reconciliation_complete=true`、`unreconciled_cost_calls=0`、`actual_usage_unknown_calls=1`；xarray 第 3 次实际用量不可见，不能声称与服务商账单一致，也不按零计费。

总调用 142 次（141 settled + 1 unknown_frozen）；Token 合计基于 141 次 settled：输入 1,441,375、缓存输入 435,072、输出 54,218；xarray 第 3 次无 usage。逐题值见上表。

### 2.5 判定边界与未执行事项

- `INVALID_ENV`：本批次**没有**实例被判为环境无效。`preflight.txt` 是更早一次探测，记录 10 个 `IMG_MISSING`；真正生效的是 `run_batch_v2.sh:50-55` 的内联 inspect 门禁，`batch-status-l2cal10c.log` 没有 `PREFLIGHT_FAIL` 且继续 START，可证明该门禁通过。实际执行镜像与实例一一对应，未套用 pvlib 修复镜像；应归档本次成功 inspect 的完整输出。
- `COLLECTION_ERROR` / `SANDBOX_ERROR`：本批次没有出现；`matplotlib` 是模型输出非法，`xarray` 是 Provider 读超时/未知费用。
- `BUDGET_STOPPED`：单题硬上限未触发；整批在未知费用处主动停批。
- 费用对账按保守上界完成：141 次 settled 调用使用服务商 Usage；xarray 第 3 次 `ReadTimeout` 无 usage、`cost=null`，按**全额预扣 0.8114688 CNY**入账，不按零计费；该账本保持 frozen，未再派发。`measured_usage_ledger_deltas` 仅记录这 0.811469 的解释性差异，`usage_ledger_deltas`（上界口径）为 0。
- 后 3 题未执行，不进入任何官方分母，也不当作模型失败。
- 批次结束后按 `codepilot=1` 过滤 `docker ps -a` 与 `docker volume ls`：**残留容器 0、残留卷 0**；未删除任何历史镜像、容器或卷。`docker-cleanup-check.txt` 只有标签/表头行（`CONTAINERS`、`VOLUMES`、`ALL_CP`），没有数据行；空表头即 0 条，建议后续补实际只读命令输出。
- 未运行 test50/test300/M4；未重跑 Phase 17 官方评测；未修改 Phase 16 `INVALID_ENV` 或 Phase 17 `VALID_EVAL/RESOLVED` 记录。

### 2.6 Artifact

- `artifacts/phase18/l2-cal10/manifest.json`
- `artifacts/phase18/l2-cal10/cal10-results.json`
- `artifacts/phase18/l2-cal10/cost-ledger-reconciliation.json`
- `artifacts/phase18/l2-cal10/official-evaluation-summary.json`
- `artifacts/phase18/l2-cal10/phase18-summary.json`
- `artifacts/phase18/l2-cal10/batch-status-l2cal10c.log`
- 每题的 `reports/`、`predictions/`、`eval/`、`cost/`、`usage/`、`journal/`、`dispatch-ledger-l2cal10c.json`、`run-classification-l2cal10c.json`、`smoke-l2cal10c.log` 与 run 级 `events/llm_requests/llm_calls`。
- 运行现场（含 workspace、repo-cache）保留在 WSL `/root/cp-run/phase16/CodePilot-Repair/artifacts/phase18/l2-cal10/`，未复制到 Windows 仓库，避免把大体积工作区混入交付。

---

## 3. 过程事件与披露

1. **早期 Cal10 尝试**：compaction 前曾用 label `l2cal10` 跑过一次 astropy，模型别名写成了 `glm-5.3-flash`（服务端不存在），在建模前失败；该次 `LIVE_NOT_RUN`、0 调用、0 费用、无账本，不计入 Cal10。该失败报告保留在 `astropy__astropy-12907/reports/swebench-smoke-l2cal10-...json`。
2. **首次 astropy 工作区失败**：生产 `WorkspaceManager` 默认缓存为完整历史 clone，首题因“32966 个可达提交”在模型前停止；随后改为按实例预置单提交浅缓存，未产生模型调用。
3. **Phase 18 准备期事故**：一条 WSL 命令的 shell 变量展开为空，`git -C "" ... reset --hard` 误作用于 Windows 仓库，工作树被切到 pvlib base commit。通过保存现场、恢复分支到 `d5734ad`、回拷 Phase 18 文件完成恢复；Phase 16/17 在四份同步文档中的未提交文字损失不可恢复，已在上一版本记录中披露。Phase 16/17 的独立交付记录与 artifact 未受影响。
4. **旧 label 报告**：`swebench-smoke-l2cal10-...json` 是第 1 条所述失败，不是本次 Cal10 有效运行；本报告不把它并入分母。

## 4. 未关闭风险

1. **xarray 实际用量不可见**：`ReadTimeout` 后 BigModel 是否实际计费无法从客户端确认；账本保留并已按 `0.8114688` CNY 预留上界入账，不能声称这是服务商实际账单。账本保持 frozen，后续派发前仍不得重置、拆分或按零计费。
2. **3 题未执行**：pylint、pytest、scikit-learn 没有预测/补丁/官方判分；本报告不给出这 3 题的任何结果。
3. **内部 M2 目标测试授权缺口**：astropy、django 的 `failure_reasons` 均为 `no authorized target test available`，内部链路未完成目标测试自评；这不是环境故障，也不影响官方判分，但会拉低内部链路形成终态率。
4. **官方解决率样本很小**：5 个有效官方评测，40% 只是本批读数，不是 test50/test300/M4 结论。
5. **Phase 17 修复镜像只适用于 pvlib**：本批 10 题均使用各自官方镜像，未套用 pvlib override。

## 5. Git 状态（历史快照，2026-10-22）

- 分支：`codex/phase12-real-issue-pilot`
- HEAD：`d5734ad`
- 未 commit、未 push、未创建 PR。
- Phase 18 新增/修改：生产镜像解析与 `OfficialInstanceSandbox`、`scripts/swebench_smoke.py`、`tests/unit/sandbox/test_instance_image_resolution.py`、`configs/swebench/instance-image-overrides.json`、`artifacts/phase18/` 与四份同步文档。

---

## 6. Phase 18E：l2cal10e 追加尝试与 P2 收口（2026-10-23/24，最新）

> 本节是当前最终口径。第 1–2 节保留为原 Cal10 历史快照，不覆盖、不改写。

### 6.1 口径分层

| 层 | 范围 | 当前结论 |
|---|---|---|
| 原 Cal10（l2cal10c） | 10 题，7 题真实执行 | 历史快照：有效官方评测 5，RESOLVED 2，40.0%；xarray 未知费用停批 |
| l2cal10e 追加尝试 | pytest + sklearn 两个实例，a3–a21 及 base 共 25 条 registry | pytest P2 `NOT VERIFIED`；sklearn 内部 `ACCEPTED`、官方 `UNRESOLVED` |
| 官方 Harness | 只认 Harness verdict | pytest 未运行 → `NOT_EVALUATED` / `DEFERRED`；sklearn 既有 `UNRESOLVED` |
| 内部验收 | M2 closure 的 acceptance | pytest 最后一次 `l2cal10e-a21` → `REJECTED`；sklearn a7 recheck → `ACCEPTED` |

**不得混用**：内部 `REJECTED/ACCEPTED` 不等于官方 `UNRESOLVED/RESOLVED`；pytest 的官方字段只能写 `NOT_EVALUATED` / `DEFERRED`，不得写成 `UNRESOLVED`、`RESOLVED` 或「已评测」。

### 6.2 最后 attempt：l2cal10e-a21

- 真实 Docker 图运行已发生：`run_id=swebench-smoke-l2cal10e-a21-pytest-dev__pytest-11148-s0-6f621582`，`model_run_status=LIVE_RUN`，`status=SMOKE_COMPLETED`，4 次迭代、3 个已保存 attempt、14 次 test run、59 次 LLM 调用。
- 停止原因：`stop_reason=repeated_patch`（manifest 记为 `deterministic coder repeated`）。模型没有得到满足六项验收的补丁；**不是预算耗尽、环境故障或 Harness 故障**。
- 目标测试已执行但未通过：`target_test_passed=false`；完整回归未完成：`regression_clean=false`；Reviewer 未执行：`review_executed=false`、`review_bound_to_candidate=false`。
- 补丁已生成且通过补丁校验：`patch_present=true`、`patch_validator=true`；`final_patch_sha256=4b7f04c32cad72a5e2b0d22512a562b674ab23dd55bababceb128a9cb4994b2f`。
- 内部验收：`accepted=false`、`verdict=REJECTED`。
- 官方字段：`harness.decision=DEFERRED`、`harness.verdicts[0].verdict=NOT_EVALUATED`、`report_path=null`、`counts_toward_resolved_rate=false`；没有 pytest 官方 Harness 结果文件。

### 6.3 sklearn 既有结果（本轮未重跑）

- 最后一次：`l2cal10e-a7`（sklearn）。
- 原始 `acceptance-l2cal10e-a7.json` 为 `REJECTED`（`review_approved=false`）；随后 `acceptance-l2cal10e-a7-recheck.json` 为 `ACCEPTED`。本文以 recheck 作为内部验收结论，并保留原始文件供追溯。
- 官方 verdict：`UNRESOLVED`（`official-evaluation-l2cal10e-a7.json`，真实 Harness 完成）。
- 本轮没有重跑 sklearn；不得把 a10–a21 的 pytest 结果写进 sklearn。

### 6.4 未重跑范围

- `pydata__xarray-4493`、`pylint-dev__pylint-6506`、原 `l2cal10c` 的既有结果均未重跑。它们的旧记录仍按历史快照保留。
- 原 Cal10 的 xarray 未知预留 `CNY 0.8114688` 仍在保护集合内，不得重置、拆分或按零计费。

### 6.5 金额（只读账本口径）

| 指标 | 金额 CNY | 来源 |
|---|---:|---|
| pytest 单题累计上界 | 7.80709616 | a21 batch status `instance_total` / a21 ledger `accounted_or_reserved` |
| sklearn 单题累计 | 0.62403504 | registry / phase18e 旧汇总 |
| 批次已结算 | 7.18344160 | a21 批次结算口径 |
| 未知预留（5 次，每次 0.8114688） | 4.05734400 | a21 recovery manifest |
| 批次累计上界 | 11.24078560 | 已结算 + 未知预留 |
| pytest 剩余单题预算 | 12.19290384 | a21 batch status `instance_remaining` |
| 批次剩余预算 | 88.75921440 | a21 batch status `remaining` |

- a21 本次：已结算/上界均为 `0.44591856`，`unknown_cost_calls=0`。
- 未知预留 5 次**绝不按零计费**；`reconciliation_complete=true`、`unreconciled_cost_calls=0`、`actual_usage_unknown_calls=5`。
- 20/100 护栏未触发（a21 ledger `limit=20`，requests=59；停止原因是 `repeated_patch`，不是护栏）。

### 6.6 受保护文件与 registry

- 受保护文件哈希：**12/12 一致**；证据 `artifacts/phase18/l2-cal10e/preflight/protected-verification.json`（`all_match=true`）与 `protected-before.sha256`。
- `attempts.json`：25 条 registry，其中 pytest 21 条、sklearn 4 条；25/25 账本存在且 SHA-256 与 registry 一致。
- 注意：`attempts.json` 按 `(instance_id, attempt_label)` 字典序排列，不是按创建时间；pytest 段末尾是 `l2cal10e-a9-resume`，全数组末尾是 sklearn 的 `l2cal10e-a7`，两者都不是 a21。最后 attempt 以 batch status 与 `recovery-manifest-l2cal10e-a21.json` 的 `source_run_id` 为准 = `l2cal10e-a21`。
- `attempts.pre-a10-a21.json` 是 a10–a21 之前的 12 条冻结快照，用于对比，不是最终 registry。

### 6.7 旧汇总的适用范围（重要）

以下三个顶层文件只覆盖 **a9 时点**（`cost-ledger-reconciliation-l2cal10e.json` 与 `phase18e-summary.json` 的 `generated_at=2026-10-22T19:16:40+00:00`；`official-evaluation-summary-l2cal10e.json` 的 l2cal10e 段同为 a9 口径），**不能充当 a10–a21 的最终汇总**：

- `artifacts/phase18/l2-cal10e/cost-ledger-reconciliation-l2cal10e.json`
- `artifacts/phase18/l2-cal10e/official-evaluation-summary-l2cal10e.json`
- `artifacts/phase18/l2-cal10e/phase18e-summary.json`（其中 pytest `final_attempt=l2cal10e-a9`、`p2.status=AWAITING_ADDITIONAL_BUDGET`）

最终口径指向：

| 用途 | 文件 |
|---|---|
| batch 级费用/停止入口 | `artifacts/phase18/l2-cal10e/recovery-manifest-l2cal10e-a21.json` |
| a21 派发账本 | `artifacts/phase18/l2-cal10e/pytest-dev__pytest-11148-a21/dispatch-ledger-l2cal10e-a21.json` |
| a21 内部验收 | `artifacts/phase18/l2-cal10e/pytest-dev__pytest-11148-a21/acceptance-l2cal10e-a21.json` |
| a21 run 报告 | `artifacts/phase18/l2-cal10e/pytest-dev__pytest-11148-a21/reports/swebench-smoke-l2cal10e-a21-pytest-dev__pytest-11148-s0-6f621582.json` |
| a21 批次状态 | `artifacts/phase18/l2-cal10e/batch-status-l2cal10e-a21.log` |
| 新版 registry | `artifacts/phase18/l2-cal10e/attempts.json` |

SHA-256（只读核验用）：

| 文件 | SHA-256 |
|---|---|
| `attempts.json` | `20a2fa70e3108f6acbff3b361d3312c4941a4a0a2d87f0b28b546dd17134b1b8` |
| `recovery-manifest-l2cal10e-a21.json` | `068f326df68f6e5bc24a75d43c6c338bcf2234a8650629b023298b671b8defdf` |
| `batch-status-l2cal10e-a21.log` | `b919dcbdef2858435c473676d3d98c3c24e0d259f92a5cafb650081c72b97b65` |
| `acceptance-l2cal10e-a21.json` | `106ec4d66b51eba760d20f38bf4548af959eac9f598542d15d3bb41b791b13c2` |
| `dispatch-ledger-l2cal10e-a21.json` | `c3d43260522f7358f4a4381eafe261b648d72ede9b5ba63475893cc9230e0a91` |
| a21 run report | `cff3eb54b3ddfaa0cf5b9c26c5fe3b7715303338d1bc60de57a0e55b2d7131e6` |

### 6.8 a10–a21 的生产修复口径（静态记录）

本节只记录实现口径，**本轮没有重新执行任何测试、Docker、Harness 或模型调用**；实际运行结果仍以第 6.2–6.5 节的证据为准。

| 变更 | 口径 |
|---|---|
| 目标测试选择 | Planner 的已有目标优先；为空时依次回退 reproduction / impact / baseline / related。文件级目标只展开为 base 上 PASSED 的 NodeID；越界、命令形态、`planned:` 前缀与不存在的测试一律拒绝并记录理由。 |
| 基线范围 | 有 impact 证据时，基线只跑 impact 相关的既有测试文件（过滤 `conftest.py` / `__init__.py` 与越界路径）；L2/L3 的“完整”以该基线范围为界，并在 evidence 里记录 `scope=N explicit test targets`。没有 impact 时仍跑官方测试命令的全量范围。 |
| 回归顺序 | L2/L3 使用基线第一次运行的收集顺序，不再对 NodeID 重新排序；存储用的 `outcomes` 仍排序。L3 全量次数上限保持 2。 |
| flaky 重试预算 | 每轮所有新失败共享 `MAX_NEW_FAILURE_RETRIES=2` 次重试；预算耗尽后剩余新失败直接计为回归，不伪造通过。 |
| 官方准入 | `scripts/l2cal10e_official.py` 在调用 Harness 前重新核对内部 acceptance；非 `ACCEPTED` 一律拒绝（带同 run_id 的人工 recheck 例外）。失败输出只写 `official_verdict=NOT_EVALUATED`，不写 `ERROR` 冒充 verdict。 |
| 权限边界 | `CODEPILOT_DENY_EXISTING_TEST_EDITS=1` 只收紧既有测试文件的写入；复现目录仍是唯一例外，且补丁交付会剔除 `_codepilot_repro`。 |
| 测试额度 | `RepairBudget.max_test_runs` 默认仍为 12；`CODEPILOT_MAX_TEST_RUNS=18` 只在 a12–a21 的批次脚本中作为显式环境覆盖。 |
| 账本完整性 | `record_attempt_from_ledger` 拒绝把已有账本的记录替换成零费用行；多源 manifest 继续把 unknown/frozen 预留计入上界，绝不按零计费。 |
| 镜像 | 执行镜像按实例解析并校验完整 ID；`OfficialInstanceSandbox` 从镜像恢复缺失构建产物时拒绝 tar 路径穿越，且 `testdeps` 的 git exclude 必须位于 workspace 内。 |

### 6.9 结论与下一步

- **pytest P2 = `NOT VERIFIED`。**
- **官方 Harness 未运行；pytest 官方 verdict = `NOT_EVALUATED` / `DEFERRED`。**
- **不得继续派发 a22 或任何新实时运行。**
- 如需继续，先由人工决定：是否接受当前证据边界、是否补充预算、是否重跑 sklearn/xarray/pylint，以及是否重新设计 pytest 的补丁生成与验收路径。任何新运行都需要新的书面授权。
