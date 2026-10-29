# 2026-10-29 最终修正状态（v5，已执行验证）

目录/历史证据权限、排队后 deadline、HTTP 请求总 deadline 与可靠取消、线程/事件循环所有权、时间估算适用性与取消恢复均已完成并执行验证。完整验收证据见 [运行验证与最终验收](phase26-runtime-validation.md)，机器可读结果见 [runtime-results](phase26-runtime-results.json)，静态与归档核对见 [检查记录](phase26-static-checks.json)。新候选为 `phase26-candidate-v5`（pending，父版本 v4，无 active 指针），本地提交完成，不 push/PR。真实模型请求与新增模型费用均为 0。

下文是 v3 及更早记录，保留当时事实；其中两项“部分实现”描述已由上面的 v4 实现替代，不作为当前未完成项。v4 的保守回退和运行限制以上述新报告为准。

---

# Phase26 最新实施状态：审查修正与静态交付

日期：2026-10-28；HEAD 保持 `07625a83f7d39db3f88ddde4ebd5005851978afa`，改动未提交。

本轮完成五个审查问题的工程处理，并接入 ECC 的条件化经验选择、阶段交接摘要与有界迭代检索。实际资源门禁、准备开始/索引计数、统一 API/SSE 投影、比较导入与证据哈希校验已落地。**测试只编写、未执行；所有性能、质量、可靠性和交互收益待验证。**

权威的逐项覆盖表、调用入口、使用方法、未完成边界和最小运行验证顺序见 [完整收口报告](phase26-closeout.md)。精确静态检查命令和实际结果见 [静态检查记录](phase26-static-checks.json)。新候选为 `configs/harness/candidates/phase26-candidate-v3`，父版本链 v3 → v2 → v1 → baseline 全部保留；没有 active 指针，没有自动提升。

与上一轮相比：

- 初始化在 graph guard 内；身份错误禁止错误恢复，可选经验/投影故障不阻断既有节点与 finalization。
- 全仓查询直接旁路，不额外全读库；范围查询有内容身份、昂贵范围旁路记忆、分项计时和总字节上限；读取新增区间有明确标注。
- 经验条件真实执行，未知条件拒用，旧文本仅建议；选择/拒用/截断有审计，pending 仍 pending。
- 真正的 diff 内容变化与“已有补丁”分开；相同只读请求不制造进展。
- 新 lifecycle 事件携带后端统一投影；API/SSE 按 seq/revision/run 合并；运行计时与终态冻结分开。
- 同机跨 worker 进程执行门禁覆盖索引/构建、测试、模型与 workspace 写入；可取消排队，排队/执行时间分列。并发配置收益未经验证。
- 阶段摘要进入 ContextBuilder 预算，完整源查询可按任务内内容哈希分页取回；低收益一次诊断后返回编码，不增加额度。
- 比较导入校验原证据、两个快照和冻结条件，L3 只能引用有效比较；readiness 不是自动提升或效果证明。

具体部分实现边界：原增量索引的跨文件关联闭包边界没有重写；TestBaseline 缺少同环境/逐 selector 时间身份，因此未实现可靠的自适应时间分配。需求行为缺乏独立绑定观察时保持 unknown。详见收口报告第 9 节，不能把这些边界写成已验证完成。

---

# 上一轮实施记录（历史保留，本轮结论以上文及收口报告为准）

以下内容保留上一轮工程取舍、命令和当时静态检查事实；其中“未接入”“数量”“缓存上限”“当前快照一致”等表述描述的是 v1，不能覆盖 v3 的最新实现与检查结果。

# Phase26 实施报告：契约、探索去重、可解释恢复与离线候选

日期：2026-10-28。起始 HEAD：`07625a83f7d39db3f88ddde4ebd5005851978afa`。

本轮交付真实源码、测试代码及可审查 diff。**所有性能、修复能力、恢复成功率和交互效果均待验证**。没有运行测试、构建、Docker、Harness、真实模型、付费任务或 RSI 实验；没有提交、push、PR。Phase25 历史报告、结果、费用和原有未跟踪文件保留。

## 1. 核实与实现取舍

先读取了用户给定的 AGENTS 规则、HEAD、工作树、Phase26 方案及 Phase25 报告。在项目及适用父目录未找到额外 AGENTS.md；按用户提供的规则执行。没有启用 Superpowers。

核实到的缺口：

- `m3_batch.py` 直接官方分支在判断有效 verdict 前保存终态；非零退出还会让已有判分被外层异常处理退回未完成。
- `RunnerConfig.timeout_seconds` 已存在，但正式 assembly 未把它接入有效 EnvSpec。
- 图节点已按剩余闭环预算收紧测试 timeout，Coder 的 `run_tests` 工具原先使用装配时的固定 EnvSpec。
- 既有 Planner/Coder/Reviewer、RepairState、EventSink、checkpoint、阶段恢复、模型已结算缓存、索引快照和环境缓存均可复用。

没有重做 Phase25 已完成的原生 runner 超时归因、参数化 selector、镜像身份、费用账本和阶段恢复。没有扩大模型、服务商或任何预算。

## 2. 六项目标的实际改动

| 目标 | 本轮实现及真实接入 | 复用与效果边界 |
| --- | --- | --- |
| 准备速度 | assembly 记录索引准备耗时/复用来源和环境缓存身份；只读查询在既有 registry 中复用 | 索引快照、环境缓存本身原已存在；没有新建索引/环境服务。准备耗时收益待验证 |
| 行动效率 | Coder 每个工具回合记录重复查询、计划文件相关证据、显式陈述的假设变化、实际 workspace diff；低收益时返回短反馈；相同身份读取/完整搜索可命中 | 保留全部原有调查工具与硬额度；不强制固定轮次写代码，不自动停止必要调查。首次修改时间待验证 |
| 修复质量 | RepairPlan 增加四项契约及来源、explicit/inferred/unknown；Planner 生成、Coder 消费、Reviewer 对照；候选绑定的失败摘要进入下一次编码上下文 | 没有增加契约 Agent；缺失契约默认 unknown。模型声明及自测通过都不自动证明需求满足；质量待验证 |
| 验证效率 | 正式 runner override 生效、节点及工具派发前按预算收紧；输出阶段、预期/实际、候选、基线关系、实际/缺失范围和新增信息 | 复用既有候选/验证 artifact 身份检查与完整性门禁；没有增加“超时也算通过”的缓存；效率待验证 |
| 恢复能力 | 官方判分共用身份检查；无效结果可恢复；非零退出保留可信判分并记异常；专用 official-only 恢复入口；任务提示/经验快照固定；Coder 在工具之间确认取消 | 沿用 checkpoint/worker/阶段导入及账本，保留候选和 unknown 费用。进程崩溃窗口仍不保证 exactly-once；恢复效果待验证 |
| 产品体验 | Task API 与现有 RunPanel 贯通阶段、开始时间、阶段累计、排队/执行/模型等待、缓存理由、取消请求/确认、恢复解释及六维证据；中英文文案 | 使用现有 SSE、API 和页面，无新状态数据库或虚构完成百分比；页面与实时交互待验证 |

## 3. 文件与调用链

| 文件/位置 | 接入方式 |
| --- | --- |
| `scripts/m3_batch.py` | `_run_instance → _evaluate_official → _official_verdict`；`ingest_official` 共用判分检查；`resume_official` 只调用官方 runner，复用批次锁 |
| `schemas/plan.py`、`agents/planner.py` | 四项契约进入 RepairPlan schema 与 Planner 真实输出；带来源的主张才可标 explicit/inferred |
| `agents/coder.py`、`agents/exploration.py` | 真实工具循环注入反馈、记录观察，保留工具回合审计；每次模型请求和工具派发前检查取消 |
| `agents/reviewer.py` | 独立审查上下文包含契约及候选绑定证据；保留完整补丁与原审查前置条件 |
| `tools/registry.py`、`tools/query_cache.py`、`tools/read_tools.py` | 正式工具工厂创建任务内缓存；先检查角色，再验证参数和权限，再查内容身份；保留搜索回退；空文件与不存在分开 |
| `tools/write_tools.py`、`graph/nodes.py::_run_coder` | `TestRunBudget.before_run` 在实际工具测试前计算有效 EnvSpec；拒绝无剩余时间的派发，不执行测试副作用 |
| `sandbox/official.py`、`eval/assembly.py`、`sandbox/docker.py` | runner override → effective EnvSpec → 图/工具预算 clamp → container.wait；记录配置来源和实际 timeout |
| `schemas/state.py`、`graph/evidence.py` | 带默认值的状态字段；L0 从 RepairState 派生；摘要和展示不修改 acceptance、预算或历史结果 |
| `observability/lifecycle.py`、`events.py`、`llm/calls.py`、`llm/tracking.py` | 固定顺序只读观察者，ResourceObserver 为实际使用者；真实请求前后、工具前后和资源类别事件；异常隔离 |
| `graph/deps.py`、`graph/builder.py`、`graph/harness.py` | 运行依赖绑定钩子；节点边界由执行器提交 runtime/摘要/快照引用；task_start、before_model、before/after_tool、candidate_changed、after_validation、cancel/resume、task_end |
| `api/runtime_view.py`、`api/routes.py`、`api/reporting.py`、`api/schemas.py` | 状态/事件的只读投影；历史缺失字段返回未记录；官方结果仍来自独立已记录官方数据 |
| `web/src/api/types.ts`、`panels/HarnessStatus.tsx`、`panels/RunPanel.tsx`、两套 locale | 现有运行页面消费 API 与实时 lifecycle 事件；契约、失败摘要和覆盖范围可展开 |
| `src/codepilot/rsi.py`、`configs/harness/candidates/` | 显式离线 CLI；追加候选、查看版本、只读回放、显式选择/父版本回滚；没有实验调度器 |

所有上表 `schemas/` 等短路径均相对 `src/codepilot/`；前端目录已单独注明。`graph/harness.py` 在 `_guard` 中接入，因此普通图、正式 checkpoint 恢复和安全阶段入口使用同一集成。

### 正式 timeout 来源

1. `scripts/swebench_smoke.py` 通过现有冻结 runner overrides 构造 OfficialInstanceSandbox。
2. `InferenceAssembly._execute` 在建立工具和 Deps 前调用 `effective_env_spec`：显式 runner timeout 优先，否则使用任务 EnvSpec；**未修改任何实际运行配置文件或额度**。
3. baseline、target、regression 继续使用图节点原有剩余闭环预算 clamp。Coder 工具现在通过 `before_run` 使用相同剩余时间计算；时间不足时派发前返回错误。
4. 最终 Docker `container.wait(timeout=env_spec.timeout_seconds)` 使用传入值；`ExecutionResult.diagnostics.effective_timeout_seconds` 记录本次值。
5. `runner-effective-config.json` 保存 override 哈希、来源、原 EnvSpec 值；它表示配置上限，不等同于每一次执行后的值。实际值在 execution diagnostics。

这条链的运行效果待验证；没有用恢复脚本里的常数替代正式配置，也没有把复现/目标发现原有更短上限自动放大。

## 4. 缓存、版本与副作用边界

- 查询缓存仅覆盖 `read_file` 与 `search_code`，按参数 schema 默认值归一化，不合并大小写、regex、glob 或不同角色权限。
- 身份包括 workspace 根和内容、参数、角色/deny scope、工具版本及源码哈希、搜索引擎身份、失效 epoch。搜索前后核对可见文件内容及文件集合，新增文件使负结果失效。
- 所有写入/执行工具调用后保守清空查询缓存；外部编辑也由下次内容核对识别。不存在的文件不缓存；完整空结果、截断结果、错误分别记录，截断结果不复用。
- 搜索身份扫描有 0.25 秒、20,000 文件和 16 MiB 上限；超出则旁路缓存，执行原搜索及缺 rg/超时的 Python 回退。大仓库可能频繁旁路，**不能据此声称已解决大型仓库重复搜索成本**。读取缓存上限 8 MiB、LRU 64 项。
- rg 搜索显式限制为本仓库配置：不读取父目录/全局 ignore 或 rg config，避免其变化未进入任务身份；仓库内 ignore 文件仍参与内容身份。该搜索范围变化和跨平台表现待验证。
- 逻辑工具调用仍计入原硬额度；缓存命中不伪造真实工具执行或减少既有费用。写操作、模型请求、网络操作不做简单去重/自动重试。
- 已结算模型响应缓存、跨文件索引、环境和验证证据复用沿用原入口，没有新账本、新向量库或新调度框架。资源分类事件是现有调度的观察接缝，不新增并发配额。
- 观察者拿到递归只读视图，返回建议；执行器提交状态。revision 是串行节点边界序号，不声称实现并行乐观锁事务。
- 新任务绑定真实 prompt 文本、当前源码和经验/插件版本快照；新进程续跑核对保存哈希，恢复保存的三个角色 prompt。选择文件后来变化不会替换在途对象。Python 代码仍由既有 worker/checkpoint 管理。

## 5. 使用方法

### 新任务策略与证据

正式 `InferenceAssembly` 新任务默认启用契约、观察、任务内查询缓存和生命周期记录。无需新 Agent 或新增模型请求。

- 现有运行页面 → Run 面板 → “修复阶段与证据”：查看当前阶段/活动、耗时、缓存、取消，以及展开契约/失败摘要。
- API：`GET /tasks/{task_id}` 的 `runtime`、`cancellation_state`；`GET /runs/{run_id}/report` 的 `harness`；现有事件流中的 `type=lifecycle`。
- artifact：`harness/contract.json`、`failures/<stage>-<iteration>.json`、`exploration/<iteration>.json`、`harness/l0.json`、`harness/evidence.json`、`harness/snapshot.json`。state 仅保存有界摘要/必要引用，完整源码快照在 artifact。
- 准备复用来源：`harness/preparation.json`、`harness/environment.json`、原有 `index-reuse.json`；runner 配置见 `runner-effective-config.json`，实际 timeout 见 execution diagnostics。
- 需求行为维度保守显示 `unknown`，没有独立契约行为证据时不会从内部 ACCEPTED 或自测 PASS 自动推导“满足”。

### 官方恢复与取消

以下仅为后续获准运行时的用法，本轮没有执行：

```powershell
python scripts/m3_batch.py --catalog <原catalog> --config <原冻结配置> --artifact-root <原批次目录> --ingest-official
python scripts/m3_batch.py --catalog <原catalog> --config <原冻结配置> --artifact-root <原批次目录> --resume-official --live
```

第一条仅导入外部判分；第二条只恢复官方待评测/错误项，不调用内部推理 runner。已有可信结果先复用；无效结果保持未完成，关键失败 JSON/log 保留内容哈希副本。锁文件存在时先核实旧 PID/进程，入口不会强行清锁。普通 `run` 仍可能派发其他待修复实例，不能当成“仅恢复 Harness”的替代入口。

checkpoint 恢复继续走原 worker/assembly；事件说明复用已完成阶段、有效候选/证据与待重做阶段。取消区分 requested/confirmed：当前模型/工具结束后在安全边界停止，候选、checkpoint、已发生费用和 unknown 来源保留。并非强制终止在途外部进程。

### 离线经验与候选版本

- L0：当前 RepairState 的计划、假设、工具事实和失败摘要；`task_experience` 仅派生来源/适用范围/失效条件，供本任务 Coder 使用。
- L1：绑定仓库的经验；L2：通用建议；条目有来源、适用/失效条件、版本、pending/validated/retired 和评测接触标记。仅取少量匹配仓库的条目注入固定快照。
- L3：只接受有 `comparison_refs` 的版本比较条目。本轮未做效果比较，**没有伪造 L3 结论**。

```powershell
python -m codepilot.rsi list
python -m codepilot.rsi show phase26-candidate-v1
python -m codepilot.rsi replay <events.jsonl> --mode trace
python -m codepilot.rsi replay <events.jsonl> --mode decision
python -m codepilot.rsi archive <新版本> --proposal <proposal.json>
```

archive 要求 parent（首版可空）、code_revision、hypothesis、input_evidence、candidate_changes，可提供 instruction、三角色 prompts、experiences、static_results、future_metrics；保存代码/提示/经验快照，强制新候选 pending，不覆盖已有版本。trace 只读历史事件；decision 只重算确定性探索反馈，不能证明模型补丁或真实效果改善；CLI 没有真实效果复验子命令。

显式选择/回滚示例（仅改变后续新任务的提示/建议/经验选择，不自动应用 Python diff）：

```powershell
python -m codepilot.rsi select phase26-candidate-v1 --allow-pending
python -m codepilot.rsi rollback phase26-candidate-v1 --allow-pending
```

本轮没有创建 `active.json`，没有提升默认经验策略。上述选择 pending 版本需要操作者明确提供 `--allow-pending`；收益继续待验证。rollback 选择候选的 parent；已运行任务固定原快照，历史结果和费用不回写。源码版本回退需选择对应代码 checkout、重启 worker 并按既有 checkpoint 身份核对；首版不自动还原任意 Python 对象/代码。

默认档案根是本项目 `configs/harness`；离线 CLI 可用 `--root` 管理隔离档案。正式新任务若要使用其他档案，可设置 `CODEPILOT_HARNESS_ARCHIVE`；实际选择路径与版本保存到任务快照，恢复继续使用保存内容。

## 6. 静态自检与未执行测试

最终静态检查的精确结果记录在 `phase26-static-checks.json`。已修正首次检查发现的生产类型错误、前端字段错误、缺失导入和失败证据身份问题。

实际结果：30 个 Python 文件 AST、Ruff check 和 format check 通过；28 个生产源码/批次脚本文件 mypy 通过；前端 TypeScript noEmit 与 4 个修改 TS/TSX 文件 ESLint 通过；两套 locale JSON 解析、保留 CRLF 的 Git diff 检查通过。新增 16 个机制不变量测试函数、5 个批次测试函数（10 个参数化情形）及 1 个前端测试，全部未执行。

另以只读静态脚本核对了两份父/子候选档案、62 个归档源码哈希、当前源码与候选快照一致、三角色提示齐全、父版本引用存在、diff 哈希一致，以及 offline RSI 的导入边界；未创建 active 指针。`configs/harness/candidates/phase26-candidate-v1/candidate.diff` 包含 36 个修改/新增代码文件，供未 stage 状态下同时审查新增文件。

检查范围：修改/新增 Python 文件的 AST、Ruff check/format；按项目的生产源码类型检查范围执行 mypy；前端 `tsc --noEmit` 与修改组件 ESLint；Git whitespace/diff 审查。未导入并启动应用，未安装依赖。初次 mypy 误纳入历史测试文件，报告了原有未注解变量；后续按 pyproject 的生产源码范围检查，没有为这些旧测试做无关修改。

`graph/builder.py` 在 HEAD 与工作树原本均为 CRLF。保留原行尾，使用 `git -c core.whitespace=cr-at-eol diff --check` 校验；普通 `git diff --check` 会把新增行的 CR 当成尾随空白，未通过整文件换行来制造无关 diff。

编写但**全部未执行**：

| 测试文件 | 新增覆盖 |
| --- | --- |
| `tests/unit/test_m3_batch.py` | 无效 verdict 三类、身份缺失/冲突四类、非零退出保留有效判分、仅官方恢复跳过待推理项、复用已存在可信判分、损坏 JSON 原始副本 |
| `tests/unit/test_phase26_harness.py` | 同长度内容变更失效、新文件负缓存失效、实际写入失效、权限与 empty/missing/truncated 区分、缓存故障搜索回退、低收益探索/假设变化、历史契约默认值与来源要求、只读观察/异常隔离、候选不自动发布、父版本选择、L3 比较来源/隐藏证据拒绝、两种离线回放、时间活动分离、runner timeout、快照恢复身份、候选绑定与缺失范围、工具间取消保留修改、timeout 派发前拒绝副作用 |
| `web/src/components/panels/HarnessStatus.test.tsx` | 实时阶段证据、取消 requested 展示、无伪进度条 |

## 7. 待验证、未完成及最小后续顺序

1. 获准后先运行上述纯确定性测试及既有工具/预算/checkpoint/API 回归，核实身份、失效、取消和旧数据兼容；本轮未运行。
2. 使用合成故障验证 official-only 入口、坏 JSON、非零有效 verdict、相同候选恢复及账本不变，再在获准环境核实正式 Docker timeout。
3. 启动页面核实 SSE 更新、切换会话、取消请求/确认、历史缺字段、契约及失败摘要显示；本轮未启动前端或后端。
4. 仅在另行授权后做少量开发案例比较，固定模型/provider/预算/环境，观察首改时间、重复查询、正确需求行为、回归、耗时和费用；不得自动进入 test50/test300。
5. 候选继续 pending；有独立比较证据后才能形成 L3 或讨论默认版本提升。

具体限制：

- 探索相关性只用计划文件重叠与显式 Hypothesis 文本，是启发式事实投影；没有宣称自动理解证据。只给建议，尚未实现自动转回 Planner 的新路由。
- 大仓库搜索缓存可能旁路；内容扫描自身有开销。没有重写跨文件索引的增量依赖闭包，symbol 查询仍依赖既有索引快照；当前源码以 read/search 为准。
- 没有新建全局资源调度器或跨任务上下文缓存；现有响应、环境、索引、验证复用机制未在本轮复验。
- 准备时间现先保存 artifact，任务图阶段接入 SSE；克隆/索引尚未提供开始前的逐步实时进度。终态 state 文件的 finalize 计时是写报告前快照，最终节点结束时间在事件中。
- 模型等待计时表示模型调用链墙钟，包含现有已结算缓存查找；不是服务商纯推理耗时。准备、排队、图执行不能混为同一指标。
- 当前外部模型/工具仍须结束后才能确认取消；崩溃发生在副作用完成、checkpoint 未保存的窗口仍可能重做。没有宣称 exactly-once，也未用简单自动重试绕过 unknown 费用冻结。
- official-only 恢复保留关键失败 JSON/log 的哈希副本；Harness 内部大日志布局沿用既有 runner，不承诺新增完整目录级版本库。
- RSI 支持提示/经验版本选择与代码快照留档；不自动切换 Python 实现、不自动比较效果、不发布。没有运行证据，L3 留空；所有新增候选保持待验证。

以上限制不影响本轮源码与静态收口交付，但必须在任何收益报告之前补齐相应运行验证。

## 8. Phase26 执行与验收结果（2026-10-29 最终）

本节的结论替代第 7 节“待验证”列表的执行状态；第 7 节保留为当时的实施边界记录。

- HTTP 请求总 deadline：`openai_compatible._send_before_deadline` 单请求单调时钟总时限，分块不重置；超时关闭该响应、保留共享 client；`retry.call_with_retry` 的 `before_retry` 防止超时后二次派发覆盖 unknown。10 个本地回环测试通过。
- 线程/事件循环所有权：节点私有循环只跑节点同步 I/O，任务循环独占 checkpointer；EventSink 用 RLock 保证 seq/JSONL/投影原子；取消时等待工作线程结束。校验器修复 `await aiosqlite.connect`；ownership 5 tests、resume 16 tests 通过。
- 时间估算与恢复：建议余量不再作为事实下界，历史 250s + 建议 320s + cap 300s 仍派发；未派发不计数；closure 预留只扣一次；取消后从有效 checkpoint 恢复且不重放。estimation 5 tests 通过。
- 最终回归：A 113 passed、B 85 passed、C 439 passed、D 前端 14 files/50 tests passed；A 与 C 有两文件重叠，不合并计数。
- 性能：81 文件合成仓库冷索引 0.923s、热 0.1931s、单文件变化 0.4496s；小仓结果不外推。
- 页面端到端：6 个场景通过（含 queued/running 取消、SSE 重连、run 切换），0 console error，0 真实模型请求。
- 静态：50 Python AST/Ruff/format、42 mypy、TS noEmit、5 文件 ESLint、CRLF-aware git diff 全通过。
- 归档：v5 57 源文件，diff 反向应用通过，snapshot/diff 哈希核对通过，6 个版本 259 条归档哈希验证通过；候选保持 pending。真实模型能力（流式延迟、账单对账、Docker/官方 Sandbox、大仓库收益）仍待授权环境验证。

