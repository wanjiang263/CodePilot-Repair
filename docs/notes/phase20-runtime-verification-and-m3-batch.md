# Phase 20：运行验证、零模型对照与 M3 六实例批次

日期：2026-10-24  
评测代码：`28892a70e408a7cd77c5b7f535199d072ee244d0`  
批次后可靠性修复：`b465fba91429b944f34040d042268a5161132ffb`  
唯一项目根目录：`C:\Users\lenovo\Desktop\CodePilot-Repair`  
提交策略：本地 commits，未 push、未创建 PR。

## 0. 结论先行

1. Phase 19 定向验证已执行完成：50 项定向测试全部通过，Windows workspace 62 项通过，关联离线范围 802 项通过；另有 6 项 Windows 原生平台差异和 3 项平台 skip，逐项保留了原因。
2. a21 的静态归因得到 Docker 实测支持：历史 8 个失败节点在字典序重放中全部失败，在 pytest 收集序重放中全部通过，收集序整组 168/168 通过。
3. a18 的解析回放与 Docker 重放均支持其静态归因：L2/L3 各 385 个节点，均为 368 passed + 17 skipped、exit 0、无 failed。
4. a12 的 Reviewer 上下文与有界纠正由 Fake/Mock 4 项测试支持；未单独发起真实模型调用。
5. M3 参数化入口已实现并通过 16 项成功/失败/恢复离线测试；默认 dry-run，真实运行必须 `--live`。
6. 6 个实例在真实模型前全部冻结；4 题因环境契约无效未派发，Sphinx 内部 REJECTED，Matplotlib 无补丁。没有任何实例达到内部 ACCEPTED，因此官方 Harness 一次也未运行。
7. 本轮新增 settled 费用 `CNY 0.25156496`，unknown `CNY 0.00`，新增上界 `CNY 0.25156496`，新增授权剩余 `CNY 29.74843504`。历史 opening `CNY 11.24078560` 保持原值。
8. Matplotlib 的零补丁归因于实现缺陷：Planner 的 `search_code` 在大型 workspace 中触发 rg 固定 15 秒超时，异常在节点边界被归为 `harness_internal_error`。批次后已修复为超时回退到受策略约束的 Python 搜索，并有测试；未对 Matplotlib 发起第二次付费尝试。

## 1. 工作目录与环境

- 唯一源码、Git、配置与 artifact 根目录为 Windows 项目根目录；未进入 `/root` 执行项目，未维护第二份 WSL 源码副本。
- Phase 19 验证在 Windows 原生 Python 与 Linux `/mnt/c/Users/lenovo/Desktop/CodePilot-Repair` 的定向场景中完成；Docker 容器挂载当前 Windows 项目目录。
- 正式批次使用 Anaconda CPython 3.12.7 加项目 `.venv` 依赖；Docker Engine 29.8.2；SWE-bench Harness 4.1.0。
- 官方镜像拉取经由内容寻址代理，按官方 amd64 manifest digest 拉取。最终官方 tag 的本地 image ID 与官方 manifest digest 一致。
- 官方 Harness 的 Linux 运行环境位于项目内 `.venv-linux` / `.venv-swebench-linux`；Docker API 通过仅绑定 WSL 适配器的临时 relay 提供，批次结束后关闭。未修改 Docker Desktop 全局代理设置。

## 2. Phase 19 运行验证

### 2.1 定向结果

| 范围 | 结果 |
|---|---|
| Phase 19 五组定向测试 | 50 passed, 0 failed, 0 skipped |
| Windows workspace 与 junction | 62 passed |
| 全部相关离线回归 | 802 passed, 6 failed, 3 skipped |
| Linux transfer 复测 | 20 passed |
| ruff check / format check | PASS |
| mypy src | PASS, 196 files |

关联回归的 6 个失败：

- 5 个 `tests/unit/sandbox/test_transfer.py` 失败依赖 Unix symlink 与可执行位；同一文件在 Linux `/mnt/c` 下 20/20 通过，属于 NTFS/Windows 语义差异。
- 1 个 workspace cache rename 在整套运行时遇到临时 `WinError 5`；单独复跑 `test_checkout_semantics_are_pinned` 为 1 passed。

没有删除测试、弱化断言、deselect 或新增无理由 skip。

### 2.2 验证中修复的实现缺陷

- acceptance 的目标来源在 root report 与 attempt 均缺失时回退到 closure；避免真实报告被误判为无目标。
- schema 1 recovery 校验改为把附加已结算账本计入总额，并拒绝同一账本重复列示。
- `InferenceAssembly` 的 `reproducer_policy` 作用域错误修复；否则真实 Reproducer 路径会 NameError。
- Windows local Git fallback 修复：复制模式 MSYS 回退、annotated tag 对象复制、重复对象只读覆盖、非 Git 本地路径前置报错。
- M3 预检全不可用时保留具体 `ENV_ERROR` 和 stopped 汇总，不残留 dry-run 假状态。

## 3. 零模型对照重放

独立 artifact：`artifacts/phase20/zero-model-replay-r4/`。  
镜像：`swebench/sweb.eval.x86_64.pytest-dev_1776_pytest-11148:latest`  
完整 ID：`sha256:b8eef9af40c714067a5455e8d88620439b536a5455fed96c5646b4ed37028442`  
模型调用：0；官方 Harness：0。

### 3.1 a21

- 同一 base `2f7415cf…`、同一最终补丁 `4b7f04c3…`。
- 字典序：168 节点，159 passed / 9 failed。
- pytest 收集序：168/168 passed。
- 历史 8 个失败节点：字典序下全部 failed，收集序下全部 passed。
- 结论：顺序足以解释历史 8 个假失败；字典序重放还多出 `test_visit_sort` 失败，不把历史最终运行夸大成 9 个。

### 3.2 a18

- 历史 L2 原始：`status=FAIL`、`exit_code=0`、368 passed、0 failed、17 skipped。
- 把它按历史前置形状重建为 `ENV_ERROR` 后经现行重分类函数得到 `PASS`，`error_reason=null`。
- Docker 重放 L2：385/385 节点，368 passed + 17 skipped，exit 0。
- Docker 重放 L3：385/385 节点，368 passed + 17 skipped，exit 0。
- 结论：静态归因得到运行支持；历史 pytest Agent 闭环仍为 `NOT VERIFIED`，不因零模型重放变成新的闭环成功。

### 3.3 a12

- Reviewer 请求包含实际 `diff --git` 与 patch 上下文。
- 错误引用会触发一次有上限纠正；第二次成功。
- schema 与 semantic 连续失败均在 2 次后停止，归因 `model_output_invalid/model`。
- provider 失败不重试、不冒充模型输出，归因 `llm_provider_error/provider`。
- 4 passed；无真实模型。

## 4. M3 入口

代码：`scripts/m3_batch.py`，默认 dry-run，`--live` 才真实派发。  
复用：`swebench_smoke`、`l2cal10e_internal`、`l2cal10e_acceptance`、`l2cal10e_official`、dispatch recovery/registry。

硬约束：

- 冻结 batch_id、catalog SHA、config SHA、代码 SHA、opening manifest SHA、数据集 revision 与 seed。
- 历史 opening 和本轮新增额度分别记账；本轮 30 元新增授权、6 题主批 24 元上限、单题 4 元上限。
- 每次 HTTP 前由 `DispatchBudget` 预扣；provider `max_retries=0`；temperature 0.0 显式进入请求体；服务未声明 seed，批次记录为未下发。
- registry 写失败立即停批，不进入 acceptance/official。
- unknown/frozen ledger 立即停批。
- 内部 acceptance 重算；tested/reviewed/delivered/prediction 身份绑定。
- actual prediction 字节与 delivered patch 在官方前再次核对。
- 重复启动只跳过已 terminal 实例；已派发但未完成实例拒绝第二次付费尝试；空 ledger 的预派发中断才允许恢复。
- 全不可用预检不进入模型阶段；部分不可用实例保留在分母并继续其他可用实例。

离线测试覆盖成功、默认 dry-run、无补丁、内部拒绝、环境失败、费用未知、registry 写失败、run_id 歧义、prediction hash 不匹配、重复启动、预算不足、取消、预派发恢复、已派发中断拒绝、全环境不可用、no-retry/temperature 控制，共 16 passed。

## 5. 六实例冻结

- 数据：`princeton-nlp/SWE-bench_Lite` test，revision `6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2`。
- snapshot SHA-256：`5c7a40b4812a0d3daaffbbd63664fbf1cd8291ed520c80e304aa5c1b7e21ff7a`。
- seed：`20261024`。
- 算法：排除历史 artifact 中出现过的实例；每个仓库最多一题；按 `sha256(f'{seed}:{instance_id}')` 升序取前 6。
- 只使用 `instance_id`、`repo`、`base_commit`、`version`、`environment_setup_commit` 公开元数据；未使用 patch、test_patch、FAIL_TO_PASS、PASS_TO_PASS 或 hints_text。
- 排除的已用 test 实例 10 个：astropy-12907、django-11910、matplotlib-23314、seaborn-3010、flask-4045、requests-2148、xarray-4493、pylint-6506、pytest-11148、scikit-learn-12471。
- 冻结 catalog SHA-256：`9c6c747ecc31e34beba3b63777f6ee94a30f4a3f0b21c9287619d0e62595b168`。
- config SHA-256：`2002da875e40cbd02d1fd9c2d293278fe3ab5b00977f9db0fffc6f7d9a7fc602`。
- opening SHA-256：`f1088aa87f19116e3e7599e12a5be32fa990cf418e9675e7936240cfb785cc5b`。

选择结果：

| instance | repo | base | image ID |
|---|---|---|---|
| django__django-11422 | django/django | df46b329… | sha256:d8703d50… |
| sphinx-doc__sphinx-8273 | sphinx-doc/sphinx | 88b81a06… | sha256:11f6809c… |
| sympy__sympy-22714 | sympy/sympy | 3ff4717b… | sha256:f8f6b7d2… |
| matplotlib__matplotlib-18869 | matplotlib/matplotlib | b7d05919… | sha256:5a237c23… |
| scikit-learn__scikit-learn-10297 | scikit-learn/scikit-learn | b90661d6… | sha256:c48ade71… |
| psf__requests-3362 | psf/requests | 36453b95… | sha256:d1c8c15e… |

镜像均为 linux/amd64；最终本地 image ID 与官方 amd64 manifest digest 一致。代理只改变下载路径，不改变内容身份。

## 6. 环境预检

| instance | 结果 | 实际原因 | 派发 |
|---|---|---|---|
| django-11422 | ENV_ERROR | 官方自定义 `runtests.py` 不符合 pytest/JUnit 契约；环境还缺 `pytz` | 否 |
| sphinx-8273 | PASS | tox 的 conda PATH 修正后 `collect-only` exit 0 | 是 |
| sympy-22714 | ENV_ERROR | `bin/test` 忽略 JUnit 参数，runner 契约不受支持 | 否 |
| matplotlib-18869 | PASS | 补入镜像生成的 `.so` 与 `_version.py` 后 pytest 收集 exit 0 | 是 |
| scikit-learn-10297 | ENV_ERROR | 全新 workspace 的收集超过 900 秒上限 | 否 |
| requests-3362 | ENV_ERROR | pytest 7.4.4 在收集 `TestSuperLen` 时 TypeError | 否 |

过程中修复：大镜像 20 分钟拉取超时改为可恢复拉取；conda PATH；Matplotlib 生成物 overlay；preflight cache 按生产 GitHub repository 身份创建；相对 source 路径绝对化；收集超时按题记录。所有无效实例保留在冻结清单和 6 题分母，没有换题。

## 7. 真实模型主批结果

评测代码 `28892a2`。服务端模型实际为 `glm-5.3-flash`，配置 alias 为 `glm-5.3-flash-main`；temperature 0.0；未下发 seed；HTTP 重试 0。

| instance | 派发 | 内部 | 官方 | 关键结果 |
|---|---:|---|---|---|
| django-11422 | 否 | NOT_RUN | NOT_EVALUATED | 环境无效 |
| sphinx-8273 | 是 | REJECTED | NOT_EVALUATED | L1 PASS、回归 new_failures=0；test-runs 12/12 耗尽，L3 因 flaky retry 不完整，Reviewer 未执行 |
| sympy-22714 | 否 | NOT_RUN | NOT_EVALUATED | 环境无效 |
| matplotlib-18869 | 是 | REJECTED | NOT_EVALUATED | 无补丁；Planner rg 15 秒超时被误归因 harness_internal_error |
| scikit-learn-10297 | 否 | NOT_RUN | NOT_EVALUATED | 环境无效 |
| requests-3362 | 否 | NOT_RUN | NOT_EVALUATED | 环境无效 |

指标：

- 有效官方解决率：`null`（RESOLVED + UNRESOLVED = 0）
- 官方评测覆盖率：`0 / 6 = 0.0`
- 任务级解决率：`0 / 6 = 0.0`
- 内部闭环通过率：`0 / 6 = 0.0`
- 环境无效率：`4 / 6 = 0.666666…`

Sphinx 的补丁身份一致，L1 与回归新失败检查成立，但六项验收中的 regression complete 与 review 不成立，不能称内部 ACCEPTED，也不能提交官方 Harness。Matplotlib 没有 actual prediction，官方只能 NOT_EVALUATED。

## 8. 费用

| 项 | CNY |
|---|---:|
| 历史 opening 上界 | 11.24078560 |
| 历史 settled | 7.18344160 |
| 历史 unknown reserved | 4.05734400 |
| 本轮新增 settled | 0.25156496 |
| 本轮新增 unknown reserved | 0.00000000 |
| 本轮新增上界 | 0.25156496 |
| 含历史累计上界 | 11.49235056 |
| 本轮新增授权 | 30.00000000 |
| 本轮新增剩余 | 29.74843504 |
| 主批 24 元上限剩余 | 23.74843504 |

逐实例：

| instance | requests | input | output | cached input | settled CNY |
|---|---:|---:|---:|---:|---:|
| sphinx-8273 | 23 | 265120 | 7627 | 83136 | 0.18606408 |
| matplotlib-18869 | 13 | 86026 | 2775 | 19456 | 0.06550088 |
| 合计 | 36 | 351146 | 10402 | 102592 | 0.25156496 |

没有 unknown/frozen ledger，没有自动重试、换模或额外实例。历史和 a21 opening 未重复计入。

## 9. 本轮代码修复

- Phase 19 运行验证：acceptance fallback、schema1 多账本合计、reproducer policy、Windows workspace fallback。
- M3：dry-run/live、预算/身份/恢复/停止规则、预检门禁、默认不 fresh run。
- 沙箱：`CODEPILOT_BIND_WORKSPACE=1` 时直接挂载当前 Windows workspace 与项目内 `/out`；默认命名卷行为不变。
- 官方环境：conda PATH、Matplotlib `.so` 与 `_version.py` overlay。
- 预检：大镜像超时可恢复、逐题超时记录、生产 repository cache 身份、绝对 source 路径、pytest/tox runner 契约。
- 实测发现：`search_code` rg 超时回退到受限 Python 搜索，避免 optional accelerator 变成 harness internal error。

## 10. 测试状态

通过的定向范围见 §2 与 §4；rg 回退修复 7 passed / 1 平台 skip。  
未执行或不能声称通过：

- 全仓 pytest、全量 Docker suite：NOT RUN。
- M4、test50、test300：NOT RUN。
- a22：未派发。
- 六题均未产生官方 RESOLVED/UNRESOLVED；官方覆盖率 0。
- 历史 pytest P2 仍为 NOT VERIFIED。

## 11. 证据

- 脱敏汇总：`docs/evidence/phase20/summary.json`
- 冻结 catalog/config/opening：`configs/phase20/`
- 零模型重放：`artifacts/phase20/zero-model-replay-r4/summary.json`
- 正式批次：`artifacts/phase20/m3-20261024-formal6/batch-summary.json`、`result-summary.json`
- 预检：`artifacts/phase20/m3-20261024-formal6/preflight/preflight.json`
- 每实例 ledger、acceptance、report、events、prediction 均在对应 instance 目录。

原始请求/响应、完整日志和 workspace 不提交；提交只包含脱敏摘要与源文件哈希。

## 12. 是否进入 test50

**否。** 当前不满足扩大评测条件：

1. 官方覆盖率为 0，没有任何有效官方判分。
2. 6 题只有 2 题环境可执行；4 题是 pytest/JUnit 不兼容或收集超时。
3. Sphinx 暴露 test-run 准入与 flaky retry 未给闭环留够额度，Reviewer 无法执行。
4. Matplotlib 暴露 rg 超时误归因；修复已提交但尚无新的付费验证。
5. 需要先扩充环境兼容策略或把候选限制在 pytest/JUnit 可执行实例；重新冻结新的未见实例后才能扩大。
6. test50 至少要求：6 题预检全部有明确可执行结果、官方覆盖率大于 0、新的 rg 修复在离线/单实例受控验证通过、预算另行授权。

本轮结果只能作为 6 实例小规模工程试验，不代表 test50/test300 或泛化能力。
