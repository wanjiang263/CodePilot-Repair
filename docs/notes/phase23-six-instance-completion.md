# Phase23：原六实例系统修复与完成记录

日期：2026-10-27。唯一项目目录 `D:\Projects\Desktop\CodePilot-Repair`；WSL 仅使用该项目在 `/mnt/d/Projects/Desktop/CodePilot-Repair` 下的映射，未使用 C 盘旧项目或 `/root` 旧副本。

原六题系统阻塞清除 **6/6**，有效官方结果 **RESOLVED 3、UNRESOLVED 3**，无合法补丁 **0**，仍阻塞 **0**。sklearn 复用已核验的官方 RESOLVED，其余五题补齐真实执行和官方判分。共六个不同候选身份获得有效官方结果；同一补丁的旧阶段不重复 Harness。内部结果与官方读数分开记录，没有用官方隐藏测试反馈指导改码。

## 结果与预算

本轮新增已结算 **0.35056240 CNY**，新增未知全额冻结 **0.81146880 CNY**，新增费用保守上界 **1.16203120 CNY**。原 30 CNY 授权按全部上界累计占用 **3.19763192 CNY**，可用余额 **26.80236808 CNY**。本轮 24 CNY 上限余额 **22.83796880 CNY**；未分配给 test50。

| 实例 | 最终内部状态 | 独立官方状态 | 本轮 HTTP 请求数 | 本轮已结算＋未知上界 CNY |
|---|---|---|---:|---:|
| django__django-11422 | succeeded | OFFICIAL_RESOLVED | 6 | 0.85309264 |
| sphinx-doc__sphinx-8273 | succeeded | OFFICIAL_UNRESOLVED | 18 | 0.09983904 |
| sympy__sympy-22714 | succeeded | OFFICIAL_RESOLVED | 2 | 0.02851480 |
| matplotlib__matplotlib-18869 | succeeded | OFFICIAL_UNRESOLVED | 2 | 0.01790240 |
| scikit-learn__scikit-learn-10297 | 复用 Phase22 succeeded | OFFICIAL_RESOLVED | 0 | 0.00000000 |
| psf__requests-3362 | succeeded | OFFICIAL_UNRESOLVED | 26 | 0.16268232 |

54 次新增请求中，53 次已结算，1 次结果未知；没有把未知金额记为零。全部实例累计本轮费用均低于 4 CNY，没有按恢复段重置单题上限。

**执行偏差明确保留**：SymPy segment-2 曾因默认角色映射误走一次 `deepseek-flash`，费用 **0.022352 CNY** 已完整计入，不能算作指定 GLM 审查。修正生产路由后，segment-4 用 `BigModel/glm-5.3-flash-main`、temperature=0 补做独立 Review；合法补丁未重新生成。详见 `artifacts/phase23/model-routing-supplement.json`。

**本轮未知请求**：Django segment-1 第 5 次 Coder 请求在 120 秒后 ReadTimeout，request SHA256 `3a6b684cea583c3823fc2dbb09edf3c244d7d65402c845bce2b59df94079aa9e`。原 ledger SHA256 `0479243a3c4900bb8202a142cf500c23b18ed48004487aad459b3c992b5c031f`，仍冻结全额 **0.8114688 CNY**；没有响应 ID，供应商最终费用仍未知。已完成本地账本对账、无费用 HEAD/模型列表检查，均保留证据；未声称完成供应商结算。

后续 Django segment-3 明确引用上述旧 ledger 与 hash，预算已包含全部未知上界；仅验证原合法源码和补独立 Review，**没有重发未知 Coder 请求**。闭环审计核对该请求 hash 在本轮所有 ledger 中只出现一次。新的 Review 正常结算，未出现持续共享 Provider 故障。

历史 Phase21 Django 冻结 ledger `348d46f251f73f22cc207fda8374ca49b40a85d453199d2a6a1318daa439a046` 及其 0.8114688 CNY 未知上界维持原字节、原金额；不得与本轮这笔同额未知混为一笔。所有历史来源 hash 和新段继承 hash 已复核。逐请求金额、预留、模型、request hash、耗时和 ledger hash 见最终追加的 `artifacts/phase23/closure-r2/audit.json`；初版审计保留在 `closure/audit.json`。

## 共同生产路径修复

- `InferenceRunConfig` 贯穿装配、运行、恢复、预算、测试策略和报告；指定 alias 显式映射到所有真实角色。通过真实批次构造的共享客户端检查实际模型、temperature、timeout、trust_env 和 retries，检查发生在任何 HTTP 前。最终 GLM 请求使用冻结的 connect/read/write/pool=10/120/30/10 秒、trust_env=false、HTTP retries=0。
- `StageRecovery` 绑定原 run、instance/base、state SHA、候选补丁和所有历史费用来源；只导入可核验产物。旧 state/checkpoint 不改字节，新代码、新预算和失效阶段单独记录。原 workspace/base-only cache/index 复用，无整题重新克隆、索引或补丁生成。
- 响应缓存只允许明确已结算且输入一致的记录；冻结源账本会禁用响应缓存，**不会阻止独立可验证的补丁和测试产物恢复**。Django segment-2 的本地错误在 HTTP 前发现，修复后继续执行，费用为 0。
- 执行证据缓存增加冻结镜像 ID 绑定。相同补丁、相同目标而镜像不同，旧 FAIL/PASS 均不能直接继承；缺少镜像身份的旧证据也需重新验证。
- `prepare_candidate`/`verify_candidate` 把真实候选、instance/base、来源、原补丁字节及实际 prediction 绑定；官方准入检查合法性，不把内部 ACCEPTED 当作唯一条件。内部 edit-size 预算不扩张为官方准入条件。
- 官方入口和候选校验不再为了序列化报告加载整个 Agent/LangGraph；共享轻量序列化保留原结果结构与旧版接受流程。此修复解决了实际 WSL 评测启动的内存错误。

## 逐题根因、复用与验证

### sklearn：复用已完成结果

核验 Phase22 原 run、base、原补丁/实际 prediction、内部报告和官方文件 SHA。既有结果为有效 RESOLVED，本轮 0 请求、0 Harness 重跑。原文件位于 `artifacts/phase22/live-r3/instances/scikit-learn__scikit-learn-10297/`，新闭环只追加引用和验证结果。

### SymPy：完整候选上下文与指定模型补审

原补丁、目标和回归证据合法，旧 Review 未提供完整补丁。复用 699 字节源码补丁与既有验证，只重新执行独立 Review。审查包含完整 patch、计划、必要验证事实和改动源码；移除约 40 MB 重复检索内容，没有删掉补丁。处理 frozen ProviderBundle 的装配错误、角色错误映射和温度能力合同后，正确 GLM Review 通过，官方 RESOLVED。segment-1 为请求前本地失败；segment-3 已确认 0 HTTP 后停止，原 process.json 的 running 原文保留，停止补充记录单独保存。

### Sphinx：真实共享客户端与完整回归分派

从旧 checkpoint 明确导出并绑定原始 hash，复用已经结算的定位结果。实际批次检查共享客户端，而非只检验配置对象。真实运行形成 `sphinx/builders/manpage.py` 合法源码补丁，目标测试通过。

原完整回归把 1556 个 JUnit 参数化 NodeID 原样作为 CLI selector，转义和 HTML/引号差异导致 collection error。修复为：仅在授权 scope 完整覆盖所选文件全部 baseline 节点时，按原收集文件顺序执行整文件；结果仍严格核对原授权 NodeID 覆盖集合，不删除用例、不用额外通过数冒充完整覆盖。segment-2 只补回归和独立 Review，原补丁未改变；内部 succeeded，官方有效 UNRESOLVED。

### Matplotlib：补发现与 clean-base baseline

原改动是 `lib/matplotlib/__init__.py`，没有目标来自空定位/impact 和旧 baseline 范围不足。按实际改动模块及测试索引发现既有 `lib/matplotlib/tests/test_matplotlib.py`，有界 collect 得到四个测试，保持 pytest 收集顺序，在隔离 clean base 做两次补充 baseline，再验证候选和回归。空范围没有继续下传，原 1554 字节补丁未重新生成。随后发现 impact 字段仍空，以实际 patch hunk 对应的两个索引符号和已授权的测试范围重建并回写；segment-3 对同一补丁重新执行四项目标、L2/L3 回归与独立 Review，均通过，新增 GLM 已结算 0.008896 CNY。相同补丁的 prediction 字节和 canonical hash 均一致，复用既有官方 UNRESOLVED，不重复判分。segment-2 在请求前因原镜像缺失失败，镜像按原 digest 逐 blob 校验恢复后继续。四个现有测试是包级导入/配置回归，不能声称覆盖新增版本信息 API 的所有行为。发现和实际 collect 证据保存在 `target-discovery.json`，回写及执行证明见 `closure-r2/impact-recovery.json`。

### Requests：packed Git/base-copy、实际 Reproducer 与缺失 fixture

通过真实 `WorkspaceManagerBaseProvider`/`BaseWorkspaceValidator` 入口验证 packed 对象/base-only copy；没有只测试辅助函数。修复新隔离目录未准备 image-generated/testdeps 的问题，Reproducer 入口能真实执行。旧生成脚本在 base 上通过或出现自身异常，照实作为模型复现失败，不吞作 NO_PATCH。

segment-1 新合法源码补丁的三个目标中两个通过，第三个因镜像未安装 pytest-httpbin 而递归依赖 fixture；这才导致无效修复和重复补丁。构建固定父镜像派生环境，仅补 `pytest-httpbin==2.0.0`，保留源码/测试字节，冻结父子 image ID。三个目标在 clean base 全部通过后，segment-2 原补丁补跑目标、回归和独立 Review，旧执行缓存因 image ID 不同失效。内部 succeeded，官方有效 UNRESOLVED；同一补丁只取得一次有效官方判分。

### Django：保留合法源码、禁止测试写入、恢复闭环及官方环境

原循环多次修改已有 `tests/utils_tests/test_autoreload.py`；原混合补丁同时有 `django/utils/autoreload.py` 的合法源码改动。本轮源码部分恢复保留该源码块，明确记录排除的测试块及新旧 hash，不伪造原 checkpoint 身份。测试写保护进入类型化配置，工具写入时立即拒绝；Planner/Coder 收到实际禁止路径、源码候选和旧轮未生效原因。同一违规重复时只做有界只读重新定位，不重复整轮盲目生成；相关行为有单元验证。

另外确认 Reproducer 契约要求 pytest 顶层函数，而原生 Django runner 漏收集。仅对专用复现目录使用 pytest 解析，已有 Django 测试仍用原生 runner。固定派生镜像补 pytest 7.0.1（兼容 Python 3.6）；实际旧脚本能收集两个函数并出现声明断言失败，原生已有 smoke 也通过。

segment-1 的 Coder 请求未知冻结后不重发。segment-3 使用保留的合法源码、已结算 Planner 计划，目标 **46** 个通过、baseline **546** 节点的既有证据保留、回归无新增失败、独立 Review 通过，内部 succeeded。

首次官方执行没有创建评测容器：官方父镜像缓存缺失，自动拉取因 Docker Hub TLS 失败，报告为 ERROR，不记作 UNRESOLVED。之后一次启动在导入推理 Graph 时 OOM，亦不算有效执行。修复官方入口的无关重依赖后，从本地派生镜像导出并核验原始 blobs，官方 Docker Hub 只补缺失元数据；OCI 导入恢复 **完全相同** 的官方父镜像 `sha256:d8703d50c49fae62753520403c3d4cd8a1048b7e554159aa4f46bb5f778f0ed3`。未把派生镜像伪装成原镜像，也未改官方 Harness。新 artifact 根 `official-r3` 对同一冻结补丁取得有效官方 RESOLVED；旧 ERROR/OOM 原样保留。

Windows bind 路径的测试虽较慢，但实际闭环已成功。另行尝试的 volume 对照出现容器丢失/归档 SANDBOX_ERROR，证据保留，**没有采用该实验开关**，也未把失败写成测试通过；最终主路径维持已通过的生产装配。

## 候选身份

完整 receipt、prediction 和补丁的原字节副本位于 `artifacts/phase23/closure/candidates/`；官方结果副本位于 `closure/official/`。以下是实际交付身份，不是计划身份。

- **django__django-11422**：base `df46b329e0900e9e4dc1d60816c1dce6dfc1094e`；实际 prediction 补丁 SHA256 `9cae77deaf9f9cbc42b493f6cd38dd4e36f9c2ea849ce4848ece8b28738cd9d1`；canonical `8636c1304452c41cf3cb0fb681fa99751dd887eff18dfb9dc5affb9d5127c4fc`；官方汇总证据 `artifacts/phase23/instances/django__django-11422/segment-3/official-evaluation-r3.json`，文件 SHA256 `73c2a394b6e046f74a92b549a124cf3b26208c1288720c760f0cf15a0a4bc7f7`。
- **sphinx-doc__sphinx-8273**：base `88b81a06eb635a1596617f8971fa97a84c069e93`；实际 prediction 补丁 SHA256 `2c6109e54b73f470677a9bd1c4e4ff9289cc1d691ce45b05ffc870bf155abcea`；canonical `65ad38c88f656b9267b9f40c4838024d03311ab43c6892ea75ffd77e880a0819`；官方汇总证据 `artifacts/phase23/instances/sphinx-doc__sphinx-8273/segment-2/official-evaluation.json`，文件 SHA256 `73afcb4c5133937ee779325aa9c32224d3db1ba4d73610acbd49436a0fb8f508`。
- **sympy__sympy-22714**：base `3ff4717b6aef6086e78f01cdfa06f64ae23aed7e`；实际 prediction 补丁 SHA256 `e8b47b687fbe1c43e7fb4ac143afd187467c4fe9f1b18bef73c0179ff797ec5b`；canonical `30fecf235bd0c33accba2c69b1d09ed0ec832e6951a06a679f5343fc8274bddc`；官方汇总证据 `artifacts/phase23/instances/sympy__sympy-22714/segment-4/official-evaluation.json`，文件 SHA256 `008df07562d0eaeede221a2fc69b838760e8a3d7904b96e51c3343241dcd7b63`。
- **matplotlib__matplotlib-18869**：base `b7d05919865fc0c37a0164cf467d5d5513bd0ede`；实际 prediction 补丁 SHA256 `56177b1f36f7282dcbb8b1588ffa2d9f701a5b7eb75ad97df4bd40ea50adf29e`；canonical `ca33eff72a9fe11f313f970b7fff0996afb4300402ff20f18fda12f538efad61`；官方汇总证据 `artifacts/phase23/instances/matplotlib__matplotlib-18869/segment-1/official-evaluation.json`，文件 SHA256 `ae8240ed9e3b3973b6737398ea24ebb222358621faeb922526f462888888c1af`。
- **scikit-learn__scikit-learn-10297**：base `b90661d6a46aa3619d3eec94d5281f5888add501`；实际 prediction 补丁 SHA256 `48d96261554d791b5ea10b7bcae56a7d221dbd38ad15cb2302e24b1d524c0df7`；canonical `原 Phase22 身份核验记录`；官方汇总证据 `artifacts/phase22/live-r3/instances/scikit-learn__scikit-learn-10297/official-evaluation-phase22-six-live-r3.json`，文件 SHA256 `50a8a02a03a57f48999d253b264e5df709a463f2f8f2121311ab6abc8ed6a9e7`。
- **psf__requests-3362**：base `36453b95b13079296776d11b09cab2567ea3e703`；实际 prediction 补丁 SHA256 `29f17936d67feced725f05fd9f5ed558a4c281ae1e7f551f5ba5fde087af694b`；canonical `2bb5b9c5583e4174264d12aeac04238ba6d877e7908d5ae9d601e72a9c1b34df`；官方汇总证据 `artifacts/phase23/instances/psf__requests-3362/segment-2/official-evaluation.json`，文件 SHA256 `76e85d9ad1090cab81ee36422d2571b436d0b7b189c2f2c672460f898613ee1e`。

## 耗时

单位秒。下表为原来源 run 已知耗时、本轮各 segment 的 monotonic process 已知小计、模型等待记录小计；模型等待已经包含在相应阶段中，不能再与总时长相加。source/index 加载、测试、编码和 Review 分开保留在审计 JSON 中。

| 实例 | 原来源 run 已知耗时 | 本轮 segment 已知耗时 | 模型等待记录 |
|---|---:|---:|---:|
| django__django-11422 | 1621.667 | 1100.922 | 183.199 |
| sphinx-doc__sphinx-8273 | 未知，未填作 0 | 1193.250 | 176.834 |
| sympy__sympy-22714 | 1463.080 | 589.000 | 8.217 |
| matplotlib__matplotlib-18869 | 1021.641 | 720.671 | 24.764 |
| scikit-learn__scikit-learn-10297 | 307.214 | 0.000 | 0.000 |
| psf__requests-3362 | 168.800 | 402.031 | 204.302 |

Django segment-1：Planner 事件约 50.045 秒、Coder 约 120.168 秒（未知请求等待）；segment-3：目标测试事件约 216.493 秒、回归约 167.201 秒、Review 约 12.656 秒，其中独立 Review HTTP 等待 4.5 秒。模型 HTTP 的单次 `elapsed_seconds` 和进程 `elapsed_seconds` 使用 monotonic 计量。

事件 UTC 时钟在 Django segment-3 的短暂 validate_patch 区间出现 -0.655382 秒负差；该区间标为不可可靠计算，不当作负耗时或零耗时加入结论。SymPy segment-3 请求前停止段没有完整 elapsed，Sphinx 原旧中断段没有完整来源耗时，均明确保留未知，未冒充完整历史累计。逐段 code SHA、阶段事件推导与异常说明见 `closure-r2/audit.json` 和 `closure/code-validation.json`。

## 验证与本地提交

- 直接相关 Graph/runner/Docker/运行配置回归：**307 passed**。
- 官方去耦、旧版准入与候选合同专项：**21 passed**。
- 统一时间预算后完整 Graph＋真实 smoke 装配集成：**263 passed**，包含此前两个失败用例，未删除断言或笼统 skip。这些测试集有重叠，不把数量相加冒充独立用例总数。
- 空 impact 回写后的直接相关回归：**262 passed**；原始镜像 ID 和 11 个 blob 的 SHA256 独立核验通过。
- 最终 mypy：**203 source files，成功**；相关 ruff lint、format check 通过。未触及前端，未运行前端全套。
- 各题实际 Docker/原生 runner、实际 Reproducer 入口和真实模型/官方 Harness 证据分别保留；Fake/Mock 测试不计为真实模型效果。
- 保留用户既有未跟踪文件；只进行本地 Git 提交，不 push、不创建 PR。巨大 state/index/workspace、下载 blobs 和原始模型档案保留在 D 盘，不加入 Git。

本轮源码提交（最终归档提交另见 Git HEAD）：

```text
3dcec21 style: order impact schema imports
b53cf43 fix: type recovered impact analysis result explicitly
baa9671 fix: persist impact recovered from verified patch and test scope
5992831 style: format delivered patch identity expression
5a7a491 fix: unify review time reserve for simulated execution stages
a9bf5ea fix: keep independent official scoring free of Agent imports
0efcc92 fix: permit stage recovery while frozen response cache stays disabled
35c5043 fix: recover legal source edits and execute native-repo reproductions
0f2a3d1 fix: bind cached executions to frozen environment image
50913b4 fix: recover with source feedback and bound repeated test-edit relocalization
907ceb3 fix: preserve pytest parameter IDs through full-file regression dispatch
372a0d6 fix: bind exported stage artifacts and official candidates to Agent provenance
7d48740 fix: enforce actual GLM temperature and compact verified review input
d4be3ce fix: bind every live role to the explicitly configured GLM model
fcc02fe fix: construct immutable provider bundle with settled response cache
89aa462 fix: recover verified Agent stages and independently evaluate legal candidates
```

## test50 环境预检与运行方案

复用 Phase22 已冻结的未见 50 题清单，不重新抽样、不用本轮结果挑题；catalog SHA256 `3cfa3b13cbbf281aafb216a6942bfbdc4c249c5c082c715218ea786c2cc0c3be`，exclusions SHA256 `34ff8d16ec545e4ae62518d051c2936e7604a0e8214365695f57648f76d8cb26`，dataset snapshot SHA256 `5c7a40b4812a0d3daaffbbd63664fbf1cd8291ed520c80e304aa5c1b7e21ff7a`。

对全部 **50/50** 题完成清单、排除项、snapshot、Docker registry 描述符与本地镜像可用性核验；官方 registry 元数据可用 **50/50**。进一步做有界镜像下载和一次性隔离容器的 frozen-base checkout＋实际 native/pytest CLI 检查，2026-10-27 12:43（UTC+8）核验时，本地仍可验证 **7/50**，缺失镜像 **43/50**。此前成功记录但现已缺少对应镜像的题不计作就绪。

环境镜像初始树可能是 environment-setup commit，不能冒充 issue base；预检在隔离容器显式 checkout 冻结 base，并把 Git stderr 进度与 stdout 身份分开解析。下载遇到已记录的代理拒绝/TLS 握手超时，停止后保留断点。没有修改系统代理、叠加临时环境变量或替换成未核验镜像。完整发现结果见 `artifacts/phase23/test50-final-preflight.json`；运行方案见 `artifacts/phase23/test50-preflight/run-plan.json`。

**下一阶段可以进入环境补齐与预算审批准备，尚不能付费派发**：仍需补齐缺失固定镜像，逐题 clean workspace/index 身份和公开 Issue 导出的有界真实测试；CLI 检查不冒充真实测试通过。test50 新增模型请求 **0**，必须独立预算授权；原 30 CNY 余额不自动挪用。方案中的 200 CNY/每题 4 CNY 仅为待授权硬上限提案，不是已批准预算或费用预测。

## 尚存事项

1. 新旧 Django 两笔未知费用均仍全额冻结，需供应商最终用量才能结算，当前预算已覆盖全部上界。
2. 原六题的 UNRESOLVED 是能力结果，未据此继续烧钱、换题、换模型或修改内部状态。
3. test50 尚未全部环境就绪；下载网络和镜像供给问题、逐题真实 smoke 是下一阶段付费前的具体待办。
4. volume 对照失败和 UTC 短区间时钟异常均照实归档，不纳入主路径通过或精确累计的声明。
