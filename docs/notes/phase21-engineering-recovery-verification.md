# Phase 21：原六实例工程恢复验证

日期：2026-10-25 / 2026-10-26  
唯一项目根目录：`C:\Users\lenovo\Desktop\CodePilot-Repair`  
批次名称：**Phase21原六实例工程恢复验证批**（`phase21-recovery-6`）  
提交策略：本地 commits，未 push、未创建 PR。

## 1. 结论先行

1. **六题执行能力恢复：6/6。** 六题都在全新 workspace、官方镜像、正确 runner 下完成非零测试收集/执行；零模型验证完成 target、机械补丁、regression、Mock Review 与 acceptance 路径。acceptance 因 `simulated_llm=true` 被 `real_run` 门禁正确拒绝，未把零模型冒充 Agent 成功。
2. **真实 Agent 主批只发生了 1 次付费尝试。** Django 的 r5 尝试在 `reproduce` 阶段遇到 provider 超时，usage/费用不确定；按授权规则保留 `unknown` 预扣并冻结 ledger，立即停止后续派发。Sphinx、SymPy、Matplotlib、sklearn、Requests 未派发。
3. **官方 Harness 0 次。** 没有任何实例达到内部 `ACCEPTED`，没有 patch/prediction 可提交，官方结果全部 `NOT_EVALUATED`；官方覆盖率 `0/6`，有效官方解决率 `N/A`。
4. 本轮新增 settled `CNY 0.0475876`，新增 unknown reserved `CNY 0.8114688000000001`，新增上界 `CNY 0.8590564`；原 30 CNY 新增授权累计上界 `CNY 1.11062136`，剩余 `CNY 28.88937864`。历史 opening `CNY 11.2407856` 未重复计入。
5. Sphinx 的 `18→12` 根因是**批次配置未跨 subprocess 传递**：`configs/phase20/m3-config.json` 声明 18，`m3_batch.py` 没有把 `max_test_runs` 传给内部脚本，`swebench_smoke._build_budget` 因环境变量缺失使用默认 12。已改为类型化 CLI 传递、首次模型请求前校验、报告记录 effective budget 与来源，并拒绝 checkpoint 旧预算恢复。
6. 本轮额外发现并修复两个**非模型**系统缺陷：Django 索引在 `ImportResolver.for_scope` / test-link 提取上呈二次复杂度（>20 分钟）；索引 SQLite 大批量边写入的磁盘 journal/temp 打不开（`unable to open database file`）。修复后 Django 全量索引约 301 秒完成。另一个 M3 缓存播种身份错误（原始 `owner/repo` vs loader 的 `https://github.com/owner/repo`）导致 r4 克隆完整 53k 提交，被 base-only 隔离断言拦下，**没有模型调用**。
7. 原六实例的 Phase 20 环境/runner/工具/回归问题已解决并验证；本轮的 provider unknown 是**新的运行停止条件**，不是环境仍未恢复。

## 2. 工作目录、环境与现场

- 唯一源码/Git/配置/报告/artifact 根：Windows 项目目录；WSL 仅 `/mnt/c/Users/lenovo/Desktop/CodePilot-Repair`。没有 `/root` 副本、没有第二份 WSL 源码、没有重新 clone 项目。
- Windows：项目 `.venv`（CPython 3.12.7，pytest 9.1.1）；Linux：`.venv-linux`、`.venv-swebench-linux`。两套虚拟环境未混用。
- Docker：Desktop 29.8.2，现有 WSL2 后端。Windows 非提升 Python 进程对 Docker npipe 只有只读权限；批次通过 WSL 内 `docker.proxy.sock` 的短期 TCP relay 访问 Docker，relay 仅绑定 WSL NAT 地址，批次后停止。`NO_PROXY` 必须包含 WSL IP，否则本机 FlClash 会把 Docker API 请求代理成 502。
- WSL 的 localhost 代理提示是环境告警，不影响 `MSYS_NO_PATHCONV=1 WSL_UTF8=1 wsl` 命令。
- 电脑重启后，r1/r2/r3/r4 的中断现场全部保留；r1/r2/r4 没有 ledger、没有模型请求；r3 也没有成功完成内部运行。r5 才有第一次真实 HTTP 调用。
- r5 的 Django 现场（ledger、events、llm_calls、report、workspace）原样保留；没有删除文件、镜像、容器、卷或历史证据。

## 3. 18→12 配置传递审计

### 3.1 各层读数

| 层 | 输入/默认 | 实际 | 来源 |
|---|---|---|---|
| 冻结批次配置 | `configs/phase20/m3-config.json` `max_test_runs=18` | 18 | 文件显式声明 |
| M3 内部命令（修复前） | 没有 `--max-test-runs`，也没有设置 `CODEPILOT_MAX_TEST_RUNS` | 未传递 | subprocess 边界遗漏 |
| `swebench_smoke._build_budget`（修复前） | 环境变量缺失 → `12` | 12 | 默认值 |
| `InferenceAssembly` / `RepairState.budget` | 接收 `_build_budget` | 12 | 上游值 |
| run report / budget 事件 | `max_test_runs=12`，`test_runs=12/12` | 12 | 下游如实记录 |

结论：**不是 18 被某层覆盖，而是 18 从未离开批次配置**。属于配置遗漏，不是模型或 Harness 故障。

### 3.2 修复

- `SmokeConfig` 增加类型化 `max_iterations`、`max_test_runs`、`max_tool_calls_per_iteration`、`max_review_calls`、`runner_overrides`；CLI 显式传入，`config_payload` 与 `config_hash` 绑定 effective limits。
- `_build_budget` 由类型化字段构造 `RepairBudget`；保留旧环境变量只作为兼容回退，显式 CLI 优先，报告记录 `source`（`cli` / `default` / `environment:...`）。
- `_validate_effective_budget` 在首次模型/环境工作之前比较配置与 `RepairState.budget`；不一致 `SmokeError`，不派发。
- `resume_run` 增加 `checkpoint_budget_problem`：checkpoint 里的旧预算与冻结预算不一致时拒绝恢复，不静默沿用。
- M3 在内部运行后读取 run report 的 `budget`，与冻结 limits 逐项比较；只接受 `source.max_test_runs == "cli"`，否则停止，不进入 acceptance/official。
- 测试：显式 18 穿透装配链、默认 12、环境变量来源、CLI 覆盖、config hash、checkpoint 冲突、M3 report 校验；见 `tests/unit/test_phase21_budget_chain.py`、`tests/unit/test_m3_batch.py`。

## 4. 闭环准入、flaky retry 与 Reviewer 额度

### 4.1 Sphinx 历史时间线（Phase 20）

- `closure_admission` 在 diagnosis 时记录 `runs_spent=9 / 12`，`required=3`，`coder_share=0`，仍判 `feasible=true`；retry reserve 2 没有进入可行性判定。
- 两次 Attempt 都只到 `L1_target` + `L2_impact`；L3 未执行；`review_calls=0`。
- operations journal 显示 attempt 1 的 L2 与两个 retry、attempt 2 的 L2 与两个 retry；attempt 2 的 retry 第二次撞到 `budget_test_runs`，以 TIMEOUT 返回。
- 两轮候选补丁哈希相同（`9c20e422…`），系统仍重复跑了 L1/L2；第二次 L2 的 flaky 结果与第一次证据矛盾，最终 Reviewer 没有合法额度。
- 结论：12 次真实容器调用被“基线/复现 + 两次 L1/L2 + flaky retry”吃光；L3 与 Reviewer 没有执行空间。不是“模型没修好”的单因。

### 4.2 修复

- **L2 覆盖整个授权基线时，L2 证据即 L3**：不再重复一次全量；完成条件仍要求 L2 真实执行、PASS/FAIL 可解析、所有授权节点有结果、无重复节点。
- **flaky retry 只使用确定性闭环之外的剩余次数**：至少两次复跑才尝试分类；不足两次时不复跑，保守保留为 new failure 并在 `retry_notes` 说明，绝不把未复跑的失败写成 PASS/flaky。
- **同一候选复用合法证据**：operation journal 的 key 绑定 patch/baseline/environment/spec + scope hash；相同补丁跨 iteration 可复用，补丁变化则 identity 不同，旧证据不会复用。target 证据只在补丁哈希相同且 scope 覆盖时复用。
- **Reviewer 有 token/费用/墙钟预留**：`ClosureReservation` 增加 240 秒（2 次待审 × 120 秒）墙钟预留；测试容器 timeout 从剩余墙钟中扣除该预留，Reviewer 不被 L3 吃光时间。
- **准入与执行共享范围规则**：`_closure_test_plan` 使用真实 baseline/impact/full-run 判定；`RegressionRunner` 使用同一 `retry_allowance`/`all_authorized` 事实；不降低 FULL、不缩范围。
- 确定性测试：完整闭环、flaky retry 边界、额度不足拒绝、Reviewer 准入、补丁变化证据失效、L2=L3 去重；见 `tests/graph/test_phase9_regression.py`、`tests/graph/test_phase12_closure_admission.py`、`tests/graph/test_phase21_evidence_reuse.py`、`tests/unit/graph/test_closure_budget.py`。

> 注：admission 的 `required=3` 仍是**保守预留**（目标 + L2 + 潜在 L3）；执行时 L2 覆盖全基线可让 L3 复用 L2 证据。因此测试中的“刚好够”边界从 7/8 调整为 6/7；这是实际支出的改进，不是降低门槛。

## 5. 四类环境问题与 Sphinx/Matplotlib

### 5.1 Django

- Phase 20 日志里的 `pytz` 缺失不是镜像缺包：官方镜像包含 pytz 2024.2；真正原因是 PATH 未指向 testbed conda 时用了 Python 3.11，以及 `runtests.py` 不支持 pytest 的 `--junitxml/--collect-only`。
- 正确命令：`./tests/runtests.py --verbosity 2 --settings=test_sqlite --parallel 1 <labels>`；工作目录 `/testbed`。
- 新 `DjangoRunnerAdapter`：canonical path NodeID ↔ Django 原生 label；解析 PASS/FAIL/ERROR/SKIP、`Ran N tests`、`FAIL:`/`ERROR:` block；零测试/请求缺失/退出码矛盾分别报错；保留原始 stdout/stderr。
- 真实 preflight：67 tests，46 passed / 21 skipped，exit 0，parsed 67；r5 真实图基线执行 `test_runs=2`（两次 67 条）后在 reproduce 阶段因 provider 超时停止，未伪造 target/regression。

### 5.2 SymPy

- `bin/test` 不写 JUnit，未知 selector 会 exit 0 且 0 tests；不能靠退出码或末行关键词判定。
- 新 `SymPyRunnerAdapter`：文件级 selector、`file::test` canonical NodeID、逐测试 `ok/F/E/f/X/s` 解析、交错 warning 的 pending 状态、`tests finished` 摘要交叉校验、zero tests/exit 矛盾/超时部分结果。
- 真实 preflight：12/12 parsed，PASS；零模型 target/regression 正常。

### 5.3 Requests

- `TestSuperLen` 的 2016 年写法把 `pytest.mark.skipif(...)((cStringIO, 'Test'))` 放进 parametrize；pytest 7.4.4 把它当参数集，`len(MarkDecorator)` 抛 TypeError。降级 pytest 版本同样失败。
- 采用**实例级兼容插件**：只把这个畸形 MarkDecorator 重写为 `pytest.param(..., marks=skipif(...))`，不改测试体、不删参数、不弱化断言；通过 `testdeps` + `-p codepilot_requests_compat` 加载。
- 真实 preflight：121 tests collected；`TestSuperLen` 8 passed / 1 skipped（`cStringIO` skip）；零模型 target `test_string` PASS。

### 5.4 sklearn

- 首轮失败是 900 秒 collection 超时。根因不是单纯慢：`setup.cfg` 的 `--doctest-modules` 让全仓 collection 扫描 doctest；全新 workspace 在 900 秒内无法完成。
- 修复：runner override 给该实例一个 issue-derived 的有界 smoke scope `sklearn/linear_model/tests`；全新 workspace 下 444 tests collected，约 14.9–25.6 秒；零模型 `sklearn/linear_model/tests/test_ridge.py` 29 tests（28 passed / 1 skipped），target/regression 正常。
- Agent 运行时的 baseline scope 由 impact/related tests 决定，不是无条件全仓；L3 采样/全量仍由回归策略决定，不用 smoke scope 冒充回归范围。

### 5.5 Sphinx 与 Matplotlib

- Sphinx：preflight 1556 tests collected；零模型 baseline `tests/test_build_manpage.py` 4 tests（3 passed / 1 base failure）、target PASS、regression new_failures=[]；说明“基线已有失败”与“回归新失败”被正确区分。
- Matplotlib：preflight 8185 tests collected；零模型 4 tests PASS、target PASS、regression clean。
- `search_code` 真实入口验证（Matplotlib workspace，stub rg 触发超时）：`rg_timeout` 回退到受限 Python 搜索，50 matches、truncated、files_scanned=417、总耗时约 17 秒；`rg_missing` 回退；`rg` 非零退出保留 `search_failed`，不静默回退；deny 目录无命中。
- Matplotlib 构建产物：镜像与 workspace 的 10/10 `.so` SHA-256 全部一致，`lib/matplotlib/_version.py` SHA-256 一致（`6f74be3a…`）。

## 6. Phase 20 六个关联测试失败关闭

| 原失败 | 分类 | 最终处理 | 结果 |
|---|---|---|---|
| transfer upload exec bit | NTFS 无 Unix exec bit | POSIX 断言 exec bit；Windows 断言可表示 mode 与源一致 | Windows 17 passed / 3 precise skip；Linux `/mnt/c` 20/20 passed |
| internal symlink | Windows 无 symlink 权限 | 精确 `symlink_to` 能力探测，失败只 skip 该能力用例 | 同上 |
| escaping symlink | 同上 | 同上 | 同上 |
| special file (mkfifo) | Windows 无 `os.mkfifo` | `hasattr` 精确 skip | 同上 |
| staging LF fixture | 测试夹具 `write_text` 在 Windows 转 CRLF | 夹具改 `write_bytes` 固定 LF，断言不变 | 通过 |
| workspace cache rename | 外部扫描器/刚退出的 git 子进程短暂持有句柄 | `replace_path` 仅对 Windows 5/32 有界重试（5 次、指数退避），持续失败仍抛出 | 单测 3 passed；目标用例复跑通过 |

没有删除测试、放宽断言、deselect 或增加笼统 skip；Linux 通过没有被写成 Windows 全部通过。

## 7. 零模型执行能力验证

产物：`artifacts/phase21/zero-model/`（`summary.json`、逐实例 `summary.json`、identity supplement）。六题均：

- 全新 workspace（seed bare cache → copy checkpoint → base-only count 1 → clean）；
- 官方镜像 ref/ID 与 base commit 一致；
- 正确 runner 收集到非零测试；
- baseline 真实执行并有 PASS（不是全 SKIPPED）；
- target 真实执行；
- 机械、非测试源码补丁，身份为 canonical/raw SHA；
- 补丁后 target 仍 PASS，regression new_failures=0；
- Mock Review 绑定补丁；
- 真实 acceptance 计算：`real_run=false`，其余 checks 全部 true，verdict `REJECTED`（证明零模型不能冒充真实 Agent）。

| 实例 | baseline tests | target | regression new | acceptance |
|---|---:|---|---:|---|
| django-11422 | 67（46 pass / 21 skip） | PASS | 0 | REJECTED（仅 real_run=false） |
| sphinx-8273 | 4（3 pass / 1 base failure） | PASS | 0 | REJECTED（仅 real_run=false） |
| sympy-22714 | 12 | PASS | 0 | REJECTED（仅 real_run=false） |
| matplotlib-18869 | 4 | PASS | 0 | REJECTED（仅 real_run=false） |
| scikit-learn-10297 | 29（28 pass / 1 skip） | PASS | 0 | REJECTED（仅 real_run=false） |
| requests-3362 | 121（120 pass / 1 skip） | PASS | 0 | REJECTED（仅 real_run=false） |

零模型结果只证明执行能力/编排/补丁应用，不作为 Agent 修复能力或官方解决率。

## 8. 冻结身份

- 数据集：`princeton-nlp/SWE-bench_Lite` test，revision `6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2`；snapshot SHA-256 `5c7a40b4…`；selection seed `20261024`；原六实例。
- catalog SHA-256：`dbbde98c464caf52f9b98bb8f228f6a4176bc093f582fb8b7d64166c4d214c22`。
- config SHA-256：`c919cb32b5f2db68310dd8dca7e98b6ec32ce5f98ebd1aa58f1359557e9b6bfc`。
- r5 冻结 code SHA：`280b4660a9abae1728220f72a96d723c44ec1dcb`；r5 `batch-freeze.json` 记录 limits（max_test_runs=18）、runner kinds、runner overrides SHA、cache seeds SHA。
- 镜像：Django `sha256:d8703d50…`；Sphinx `sha256:11f6809c…`；SymPy `sha256:f8f6b7d2…`；Matplotlib `sha256:5a237c23…`；sklearn `sha256:c48ade71…`；Requests `sha256:d1c8c15e…`。
- runner：Django `django`；SymPy `sympy`；Requests `pytest_requests_compat`；Sphinx/Matplotlib/sklearn `pytest`。
- r5 的 preflight 报告沿用 r3（`0aeb83d`）生成的六实例环境/runner 结果；r3→r5 的改动只有索引性能（线性查找、内存 journal）与 M3 缓存 seed 身份，不改变环境/runner。r5 live 自身的索引与 baseline 已实际执行。

## 9. 真实 Agent 结果（r5）

批次：`phase21-recovery-6-r5`，停止原因 `Django: unknown usage/cost; ledger frozen after registry recording`。

| 实例 | 派发 | 内部 | 官方 | 关键结果 |
|---|---|---|---|---|
| django-11422 | 是（1 次） | `WORKFLOW_ERROR`（provider/usage uncertain） | NOT_EVALUATED | 5 HTTP records：4 settled + 1 unknown；¥0.0475876 settled + ¥0.8114688 reserved；baseline test_runs=2；reproduce 超时；attempts=0；无 patch |
| sphinx-8273 | 否 | NOT_RUN | NOT_EVALUATED | ledger frozen 停止后续派发 |
| sympy-22714 | 否 | NOT_RUN | NOT_EVALUATED | 同上 |
| matplotlib-18869 | 否 | NOT_RUN | NOT_EVALUATED | 同上 |
| scikit-learn-10297 | 否 | NOT_RUN | NOT_EVALUATED | 同上 |
| requests-3362 | 否 | NOT_RUN | NOT_EVALUATED | 同上 |

registry `attempts.json` 记录了 Django 的 stopped attempt 与 ledger SHA；batch-state 里后续条目保留预检/DRY_RUN 初值，权威口径以 ledger、registry、run report 与本报告为准。

### 指标（固定 6 题分母）

- 六题执行能力恢复率：`6/6`（preflight + zero-model；全部 target/regression/Mock Review/acceptance 路径可运行）。
- 官方评测覆盖率：`0/6`。
- 有效官方解决率：`N/A`（RESOLVED+UNRESOLVED=0）。
- 任务级解决率：`0/6`（不代表模型解决率为零；没有有效官方判分）。
- 内部闭环通过率：`0/6`（无 ACCEPTED）。
- 环境无效率：`0/6`（六题 preflight 全部可用；Django 停止是 provider unknown，不是环境失败）。
- 真实 Agent：1 次付费尝试，0 patch；其余 5 题因 ledger frozen 未派发。基础设施停止 1 次（provider timeout/unknown cost），模型失败 0，工具失败 0，预算停止 5（未派发）。

## 10. 官方结果与统计边界

- 官方 Harness **未运行**；六题 `NOT_EVALUATED`。
- `0` 官方判分不等于模型解决率 0；不能把 NOT_EVALUATED 写成 UNRESOLVED/RESOLVED。
- 本轮是工程恢复验证，不是未见样本泛化评测；不与 Phase 18/20 或未来 test50/test300 混算。
- 没有使用 gold patch、test_patch、FAIL_TO_PASS、PASS_TO_PASS 或 hints_text 指导推理；零模型 scope 来自 issue/public area，并在配置中标注。

## 11. 费用对账（CNY）

| 项 | 金额 |
|---|---:|
| 历史 opening 上界 | 11.24078560 |
| 历史 settled | 7.18344160 |
| 历史 unknown reserved | 4.05734400 |
| Phase 20 新增 settled | 0.25156496 |
| Phase 21 新增 settled | 0.04758760 |
| Phase 21 新增 unknown reserved | 0.8114688000000001 |
| Phase 21 新增上界 | 0.8590564000000001 |
| 原 30 CNY 新增授权累计上界（Phase20+21） | 1.11062136 |
| 原 30 CNY 新增授权剩余（上界口径） | 28.88937864 |
| 主批 24 CNY 剩余（上界口径） | 23.14094360 |
| Django 单题 4 CNY 剩余（上界口径，ledger frozen） | 3.14094360 |
| 含历史 opening + Phase21 上界（M3 `cumulative_upper_bound_cny`） | 12.09984200 |
| 含历史 opening + Phase20 + Phase21 总上界 | 12.35140696 |

- M3 的 `cumulative_upper_bound_cny` 只把 historical opening 与**当前** Phase21 批次相加；Phase20 是独立的上一段新增费用。最后一行把 Phase20 只加一次，避免重复计入 opening。
- 5 次请求：4 settled + 1 unknown；没有把 unknown 记零、没有重置/拆分/另开 attempt 绕过冻结、没有自动重试。
- 模型实际服务端 model_id：`glm-5.3-flash`（alias `glm-5.3-flash-main`）；temperature 0.0；seed 未支持/未下发；temperature 0.2、换模、best-of 未发生。
- `CODEPILOT_DISABLE_HTTP_RETRIES=1` 与 provider `max_retries=0` 生效；不计隐藏重试。

## 12. 测试与门禁

- 最终定向回归：见 `artifacts/phase21/final-targeted-tests.log`（结果以该文件为准；包含 native runner、预算链、M3、闭环准入、索引、transfer、search、fsutil、evidence reuse）。
- Phase 12 closure admission：13 passed；M3：18 passed；repo import/index/calls/test links：67 passed；transfer Windows 17 passed / 3 precise skip；Linux `/mnt/c` 20/20 passed。
- `ruff check`（Phase21 修改文件）：PASS；`mypy src`：197 files PASS；`ruff format --check`（Phase21 修改文件）：PASS。全仓 format check 仍会报告 3 个未被本轮修改的既有文件（`reviewer.py`、`target_tests.py`、`report.py`）需要格式化，本轮未做无关格式化。
- 未运行：全仓 pytest、全量 Docker suite、test50/test300、M4、a22、官方 Harness（无 ACCEPTED）。历史 pytest P2 仍为 `NOT VERIFIED`。

## 13. 剩余阻塞与下一步授权

**执行能力**：六题环境/runner/工具/回归路径已恢复并验证；没有剩余环境阻塞。

**真实 Agent 覆盖**：只有 Django 一次付费尝试，因 provider timeout/usage uncertain 停止；其余五题未派发。要继续真实 Agent 覆盖，必须由用户**书面追加授权**，并明确：

1. 是否为剩余五题创建新的执行段（新 artifact root/new ledger），每题上限 4 CNY、整段上限 24 CNY；
2. 是否为 Django 再开一次付费尝试（不能重置/绕过已冻结 ledger；需要明确追加授权与新的 ledger 段）；
3. 新段仍使用同一冻结 catalog/config/镜像/runner，或另行冻结新 code SHA；
4. 是否接受“unknown reserved 0.8114688 保留、不记零”的累计上界口径。

在没有上述授权前：**不再派发任何付费请求，不启动 test50/test300/M4/a22**。

## 14. 证据索引

- 本报告：`docs/notes/phase21-engineering-recovery-verification.md`
- 脱敏汇总：`docs/evidence/phase21/summary.json`
- 零模型：`artifacts/phase21/zero-model/summary.json`、逐实例 `summary.json`、`identity.json`
- 预检：`artifacts/phase21/m3-phase21-recovery-6-r3/preflight/preflight.json`（r5 沿用）
- 搜索工具真实入口：`artifacts/phase21/search-tool-real.json`
- Matplotlib 构建产物：`artifacts/phase21/matplotlib-artifacts.json`
- r5 现场：`artifacts/phase21/m3-phase21-recovery-6-r5/`（ledger、registry、run report、events、llm_calls；原始请求/响应不提交）
- 冻结点：`configs/phase21/m3-catalog-phase21.json`、`configs/phase21/m3-config-phase21.json`、`configs/phase21/m3-opening-phase21.json`、`configs/phase21/instance-runner-overrides.json`、`configs/phase21/zero-model-scopes.json`、`configs/phase21/repo-cache-seeds.json`
- 运行代码提交：`aa3cef8`（预算/闭环）、`cccd1c9`（runner/环境/工具）、`bb53e99`（初始冻结配置）、`0aeb83d`（索引线性化）、`2beacb0`（索引 DB journal）、`280b466`（cache seed 身份）。r5 冻结 code SHA 为 `280b466`。
