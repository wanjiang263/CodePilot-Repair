# 2026-10-29 最终修正状态（v4）

目录/历史证据权限和排队后 deadline 已修正；模型复用 provider 并发，跨文件关联闭包及同身份验证时间估算已接入。完整覆盖表、使用方式、具体回退边界见 [最终修正报告](phase26-final-corrections.md)，实际静态结果见 [检查记录](phase26-static-checks.json)。测试全部未执行，收益待验证；保留未提交 diff，不发布、不提升默认版本。

下文是 v3 及更早记录，保留当时事实；其中两项“部分实现”描述已由上面的 v4 实现替代，不作为当前未完成项。v4 的保守回退和运行限制以上述新报告为准。

---

# Phase26 实现与静态收口报告

日期：2026-10-28。基线 HEAD：`07625a83f7d39db3f88ddde4ebd5005851978afa`。

本轮在已有未提交 Phase26 改动上继续实现。未重置工作树，未删除历史证据、账本、workspace 或旧候选；未 commit、push、创建 PR。未执行测试、应用启动、构建、Docker、官方 Harness、真实模型或 RSI 实验。**所有预期收益均待验证。**

适用规则：用户提供的 AGENTS.md；项目及适用父目录未发现额外 AGENTS.md。未启用 Superpowers、安装 ECC 或插件，也未委派其他 agent。

## 1. 五个审查问题

|问题|处理结果|真实入口与边界|
|---|---|---|
|A 初始化异常|已实现：`begin` 移入 `_guard` 的异常处理，身份错误有独立异常与事件；经验和辅助投影失败回退；finalize 仍可保存原错误现场|`graph/builder.py` → `graph/harness.py`；快照缺失/哈希冲突/不支持 schema 禁止普通节点继续。finalize 不把身份错误抹成成功。可选写入使用 `write_projection`；取消后的节点 update 保留候选、usage、policy 和 runtime|
|B 搜索缓存成本|已实现：全仓查询直接走原搜索；有目录前缀的范围才做内容身份核对；昂贵或收益不足的范围在任务内记忆旁路|`ToolRegistry.execute` → `QueryCache.execute` → 原工具。身份、查找、搜索分别计时；同长度外部修改仍核对内容，权限/文件集合/祖先 ignore 纳入身份。原 rg / Python 搜索也收敛到安全目录前缀|
|C 条件没有选择作用|已实现：仓库、阶段、失败类别、runner、环境、版本和必要标签按类型执行；失效条件优先；未知结构条件保守拒用|固定 selection 每阶段重新选择；按具体匹配、仓库、验证状态、来源、ID/version 确定性排序。最多 4 条、条目 JSON 合计 6000 字符，instruction 最多 1000 字符；之后整体进入 ContextBuilder token 预算。旧自由文本仅说明，标记未执行条件和 advisory|
|D 旧补丁持续算进展|已实现：补丁存在与补丁内容变化分离；同一补丁不刷新；清空/恢复旧内容具有不同候选身份|Coder 通过真实 workspace diff hash 比较；新增计划文件相关证据与明确 Hypothesis 变化有独立依据。后端不再使用 has_modification 更新时间；重复失败摘要的派生差量字段不再制造进展|
|E 旧事件覆盖耗时|已实现：按 run_id、seq 与 revision 合并，API 水位之前的 SSE 不再覆盖；终态不继续计时|`observability/runtime.py` 是新事件的统一投影规则，EventSink 发布 runtime_projection，API 复用，前端优先消费。旧事件有兼容 reducer。组件 memoize 投影，1 秒显示时钟不重放事件；会话与 run 身份隔离|

缓存边界：文件读取身份最多 8 MiB；搜索身份最多 2000 文件、1 MiB 文件内容、单次扫描 0.05 秒。缓存最多 64 项，序列化内容总额 8 MiB，单结果超过 2 MiB 不缓存；另外两个有界元数据集合和最多 64 × 64 读取区间。Python 对象自身有固定额外开销，8 MiB 指序列化载荷而非进程 RSS。读取返回 `new_line_ranges` 与 `next_start_line`，不删掉完整原结果。全仓旁路是明确的正确性/成本选择，不宣称全仓重复搜索已加速。

## 2. ECC 实际采用与来源

只阅读以下原始文件，借鉴机制后独立实现，没有直接复制 ECC 源码，也没有运行安装器或其 hooks。

- [continuous-learning-v2](https://github.com/affaan-m/ECC/blob/main/skills/continuous-learning-v2/SKILL.md)、[session-start.js](https://github.com/affaan-m/ECC/blob/main/scripts/hooks/session-start.js)、[instinct-relevance.js](https://github.com/affaan-m/ECC/blob/main/scripts/lib/instinct-relevance.js)：采用原子建议、项目/通用作用域、相关性选择及注入上限。接入现有 `rsi.retrieve` 与任务固定快照，无第二经验库。保留 source、source_run、条件、反例等原字段。未采用置信度概率化、重复次数自动提升、后台反思模型。
- [strategic-compact](https://github.com/affaan-m/ECC/blob/main/skills/strategic-compact/SKILL.md)、[suggest-compact.js](https://github.com/affaan-m/ECC/blob/main/scripts/hooks/suggest-compact.js)：采用自然阶段边界摘要。定位→编码、失败→再编码时确定性整理用户需求、契约、事实与来源、未知点、候选/策略身份、失败基线关系和下一步。接入既有 ContextBuilder 的 plan/diagnosis 优先级与项目 token 预算。没有中途清空消息，没有附加模型调用，没有照搬 Claude 阈值。
- [iterative-retrieval](https://github.com/affaan-m/ECC/blob/main/skills/iterative-retrieval/SKILL.md)：采用已知证据→具体缺口→下一查询选项的反馈。持续低收益触发既有 `coder_relocalize` 只读诊断一轮，再返回原编码循环；不增加硬额度，不强制第 N 次写代码，不限制有理由的范围扩展。

完整工具结果保存为任务内内容寻址 `tool-results/<sha256>.json`；模型可通过 `read_evidence` 分页取回先前源代码查询，入口限制当前任务、内容哈希与当前路径权限，不开放任意日志/其他任务文件。原 reviewer 完整补丁上下文未裁去。没有明确排除证据的假设，摘要明确记录“没有记录”，不把失败自动解释为假设已排除。`model_summary: null` 明示本轮没有模型归纳。

## 3. 全量计划覆盖表

H=`tests/unit/test_phase26_harness.py`，C=`tests/unit/test_phase26_closeout.py`，B=`tests/unit/test_m3_batch.py`，W=`HarnessStatus.test.tsx`。测试均**已编写、未执行**；静态结果见第 5 节与 JSON，不能代替运行验收。

|计划条目|当前实际实现|真实调用入口|尚缺内容/边界|本轮处理方式|对应测试代码|静态验收|运行验证|
|---|---|---|---|---|---|---|---|
|§1 六目标|保持六目标与既有成功定义|现有任务/报告链|没有效果样本|不修改预算、结果与费用|H/C/B/W|源码/文档审查|待验证|
|§2 事实与直接缺口|官方 verdict/身份共同决定终态|m3_batch._evaluate_official|真实官方恢复未运行|复用上一轮补丁并审查|B|Ruff/mypy/diff|未执行|
|§3 研究取舍|小型内部机制|原 agents / graph / tools|没有论文收益迁移结论|ECC 仅机制借鉴|—|来源阅读|待验证|
|§4.1 共享状态|RepairState 权威；默认字段兼容|guard → checkpoint|无 exactly-once 保证|节点提交；缓存/投影仅派生|H/C|AST/类型检查|未执行|
|§4.2 插件边界|顺序只读 Observer；异常隔离|Lifecycle → registry/tracking|不是动态插件市场|保持现有协议，不加总线|H|Ruff/mypy|未执行|
|§4.3 快照与切换|新任务绑定，恢复核验 schema/hash；显式父版本选择|begin / RSI select/rollback|没有任意 Python 热替换|受控 worker 重启沿用 checkpoint；不在途换策略|H/C|类型/调用链|未执行|
|§5.1 Git/索引/环境/响应复用|复用原缓存与身份校验|assembly / stage_recovery / sandbox / provider|跨文件索引闭包原有边界见第 9 节|准备来源投影、正式 official-only 恢复|H/B|调用链审查|未执行|
|§5.2 搜索/读取去重|内容身份、权限、范围、字节上界、旁路、区间标注|registry → QueryCache/read_tools|全仓查询没有缓存命中承诺|直接原路径；有前缀才扫描范围；昂贵范围不重复扫描|H/C|AST/Ruff/mypy|未执行|
|§5.3 资源控制|同机 OS 锁 + 线程门禁，真正阻止并发派发|assembly index/build、AccountingSandbox、tracking、write tool/reset|不是分布式集群限流；吞吐最佳值未知|重型/测试共享串行压力槽；模型独立槽；workspace 串行；可取消等待|C|平台分支/类型审查|未执行|
|§6.1 四项契约|change/preserve/interface/discriminator|Planner → Coder / Reviewer|独立行为证明缺失时 unknown|保持来源与 explicit/inferred/unknown|H|schema/调用链|未执行|
|§6.2 探索重定向|缺口、证据、选项及一次诊断返回|Coder 工具循环|相关性是计划文件重叠启发式|实际 diff 变化与存在分离；诊断不扩额度|H/C|AST/类型检查|未执行|
|§6.3 失败与阶段摘要|候选绑定、范围缺失、基线关系、引用|evidence → _run_coder → ContextBuilder|无明确证据不编造排除假设|自然边界确定性重建；上下文预算内替换|H/C|类型/引用审查|未执行|
|§7.1 三层证据|需求行为、受影响目标范围、扩大验证范围|verification_view → API / UI details|没有独立绑定需求观察|未知保留；真实 requested/observed/missing 分列|H/C|类型检查|未执行|
|§7.2 六维独立语义|execution/behavior/regression/review/coverage/official|同一证据投影|PASS 不等于 behavior 满足|保持 acceptance 兼容；stale/skip/missing 不冒充 PASS|H|调用链审查|未执行|
|§7.3 验证时间|现有 closure reserve + effective timeout clamp|nodes/tools → AccountingSandbox → Docker|同环境细粒度自适应估计未建立，见第 9 节|保持现有保守上限；分列排队/执行/模型等待|H|类型/预算链审查|未执行|
|§8.1 官方与 runner 可靠性|有效判分保留、无效保持可恢复、timeout 来源|m3_batch / official.effective_env_spec|外部 Harness 实效未知|不重复内部模型派发|B/H|Ruff/mypy|未执行|
|§8.2 恢复决策|checkpoint、阶段导入、候选/证据身份、unknown 费用原规则|worker / checkpoint / stage_recovery|副作用完成而 checkpoint 未落盘窗口仍在|保留旧证据；取消等待不抹 usage/候选|H/C/B|调用链审查|未执行|
|§9 阶段体验|克隆前开始事件、索引计数、阶段耗时、候选、取消、策略版本|assembly → EventSink → API/SSE → HarnessStatus|展开区显示期望/当前/下次策略；无在线编辑|统一投影、memoized 合并；无 ETA|C/W|TS/ESLint|未执行|
|§10 生命周期|task/model/tool/candidate/validation/cancel/resume/end；同步调用结束 checkpoint_saved|现有真实入口|checkpoint_saved 仅声明完成的同步图调用|观察故障降级到日志，不阻断核心|H/C|静态调用链|未执行|
|§11.1 RSI 范围|离线档案，不运行模型/评测|RSI CLI|没有净收益结论|新候选 pending|H/C|导入边界审查|未执行|
|§11.2 L0/L1/L2/L3|L0 来自状态；L1 仓库绑定；L2 通用；L3 校验比较引用|retrieve → begin → Coder context|没有真实比较条目|条件执行、拒用原因、总长度上限|H/C|AST/类型检查|未执行|
|§11.3 候选流程|父子档案、配置来源、代码/提示快照、比较导入、readiness|archive/import-comparison/promotion-readiness|真实独立比较与人工提升决定缺失|不自动变更 validated 或 active|H/C|格式/哈希核对|未执行|
|§11.4 回放归因回滚|trace/decision；父版本选择；源代码单独归档|RSI CLI|决策回放不能推出模型效果|比较固定模型/provider/预算/环境/成功定义/任务集；五类指标分开|H/C|类型/路径/哈希检查|未执行|
|§11.5 污染防护|public_only；拒绝隐藏评测经验；不改成功定义|validate_snapshot/validate_comparison|真实性仍需审核原证据|有 hash 也不自动视为已证明有效|H/C|静态审查|未执行|
|§12 A–E 实施批次|上述实现已进入真实调用链|表中各入口|具体保留边界见第 9 节|复用已有能力，不以事件冒充调度|H/C/B/W|见检查 JSON|未执行|
|§13 最小运行验证|已给出次序|第 10 节|运行授权不在本轮|仅编写，不运行|测试清单|文档核对|未执行|
|§14–15 历史/状态|保留编制与上轮记录，明确本轮状态|plan / implementation / 本报告|无效果结果|新候选另存，旧档案保留|—|一致性核对|待验证|

## 4. 修改文件与接入

完整代码文件清单及哈希在新候选 `configs/harness/candidates/phase26-candidate-v3/sources.json` 和 `candidate.json`，包含基线以来的未提交代码，而不是只包含本轮增量。v2 为本轮行尾审查前的中间档案，保留不覆盖；v3 是本轮最终档案，父版本为 v2。恢复了 15 个文件的原有行尾，消除整文件噪音；保留 builder 原有 CRLF。档案采用源码白名单，不包含 configs/harness 自身或历史 logs，避免递归膨胀。

本轮新增核心文件：`observability/resources.py`（同机执行门禁）、`observability/runtime.py`（统一投影）、`harnessProjection.ts`（seq 合并）、`test_phase26_closeout.py`（补充回归测试）。其他修改入口已在覆盖表中逐项指出。`api/runtime_view.py` 保留旧导入接口，内部委托统一投影，并非无人使用的第二实现。

## 5. 静态检查及修正

精确命令、退出码、输出、文件数量和前次结果保留于 `phase26-static-checks.json`。执行范围仅为 AST、JSON 解析、Ruff/format check、mypy、TypeScript noEmit、ESLint 与 Git diff whitespace。

最终结果：36 个 Python 文件通过 AST、Ruff check 与 format check；33 个生产 Python 文件/批次脚本通过 mypy；TypeScript noEmit、5 个前端源码/测试文件的 ESLint、Git diff whitespace 均退出 0。2 个 locale JSON 成功解析。四版档案的 148 项源码哈希通过核对；v3 的 43 个文件与当前工作树规范化内容及原始字节哈希一致，候选 diff 哈希一致。baseline 是 HEAD 源码快照，本身没有候选 diff。

档案检查曾误以文件原始字节计算 snapshot_sha256，断言失败后对照 RSI 的 `_json_hash` 修正为 `json.dumps(snapshot, sort_keys=True, ensure_ascii=False)` 的 UTF-8 SHA256，再完成核对；没有修改档案内容或跳过快照检查。

本轮修正过的真实静态问题：Windows 默认 GBK 读取失败后改用 Python UTF-8 模式；新增资源类型标注、取消字段 int/bool、证据读取必填 must_exist；阶段摘要误用全局 plan 名称改为 round_plan；前端非组件导出引发 Fast Refresh 警告，移入独立 projection 文件；平台锁使用 sys.platform 静态分支。没有忽略诊断、放宽检查规则或运行测试来凑通过。

## 6. 编写但未执行的测试

- H：16 个既有本阶段测试函数，继续覆盖内容身份、角色权限、写入失效、搜索回退、契约、观察者隔离、候选 pending、L3、回放、timeout、快照、证据完整性与工具取消。
- C：18 个补充测试函数，覆盖五个审查问题、昂贵范围旁路、字节上界、阶段/环境/未知经验条件、确定性摘要、统一事件投影、重入门禁、比较原证据哈希与成功定义不一致。
- B：保留上一轮 5 个新增批次函数（10 个参数化情形）。
- W：5 个前端用例，覆盖取消请求、旧 SSE 不覆盖 API、重复乱序终态、不同 run 重连、旧补丁不制造进展。

**以上只是已编写，全部未执行。** 静态解析不能证明断言将通过，也不能证明跨进程锁、恢复或页面行为正确。

## 7. 使用方法

普通用户继续使用原任务入口。策略配置展开区并列期望值、当前任务固定值与下次任务生效值；读取 active 指针失败会显示错误且保持当前任务不变。Run 面板显示阶段、执行/排队/外部等待、耗时、当前补丁、最近有效进展、缓存原因、六维证据和取消确认；三层范围、契约、失败、准备计数等放在展开区域。终态计时停止。没有事件/字段的旧任务显示“未记录”，不补造历史。

源码查询优先指定 `path_glob: "src/module/**"`；可以有理由扩大为全仓，扩大后直接使用原搜索路径。工具结果的 `artifact_ref` 可交给 `read_evidence(reference, offset, length)` 继续阅读；这份内容是历史证据，不能当成当前文件内容。

同一任务恢复仍通过原 worker/checkpoint；仅官方恢复使用上轮文档中的 `--resume-official --live`，不会调用内部修复 runner。本轮没有执行这些命令。取消等待会在下一次 0.1 秒检查时观察到请求；这是轮询设计，不是已测得取消延迟。已经发出的网络/容器操作仍沿用原安全边界，不强杀下载。

离线比较入口（仅说明，未运行）：

```text
python -m codepilot.rsi --root <archive> import-comparison <comparison.json>
python -m codepilot.rsi --root <archive> promotion-readiness <version> <comparison-sha256>
python -m codepilot.rsi --root <archive> replay <events.jsonl> --mode decision
python -m codepilot.rsi --root <archive> rollback <version> --allow-pending
```

比较 JSON v1：顶层 `schema_version=1`、`evaluation_exposure=public_only`、`decision=accept/reject/observe`、`reason`、`evidence_refs=[{path,sha256}]`。证据路径必须在 archive 内且原文件 SHA256 一致。baseline/candidate 各包含 `version`、`snapshot_hash`（排序 JSON 的 SHA256）、`runs`（非空不重复 ID）、`cost_known=true`、`conditions` 和 `metrics`。conditions 必须完整且两侧一致：model、provider、budget_hash、environment_hash、success_definition_hash、task_set_hash；四个 hash 必须是 SHA256。metrics 为独立非负有限观测 quality、regressions、seconds、cost、tool_calls。外部数值的真实性仍需人工核实原证据；本轮没有填入任何真实比较值。

`promotion-readiness` 仅给人工审查资格，质量不得下降、回归不得增加，结果仍 pending。L3 引用已导入 comparison 的内容哈希；损坏、失联、条件不齐或原证据改变时拒用。select/rollback 仅改变后续任务提示/建议/经验，不能还原 Python 源码；代码回退须匹配 checkout 并重启 worker。**本轮不创建 active 指针，不提升任何候选。**

## 8. 待验证项

缓存在 Windows/Linux 的权限、ignore、符号链接与扫描成本；资源互斥/异常释放/取消/吞吐；新旧 checkpoint 与固定快照兼容；恢复不重复模型与费用；UI 切换 run、重连、乱序、最终计时；契约与摘要是否改善真实修复；同条件冷/热准备、质量、回归、耗时、成本比较。以上均待验证。

## 9. 部分实现与具体未完成边界

1. **跨文件增量索引闭包沿用原实现，未宣称完全解决。** `repo/index.py` 原设计保留了未变化文件对新 symbol 的部分 import/call/medium test-link 延迟更新。完整闭包需要补齐未变化文件的导入绑定/逆依赖持久化与全量对照验证；本轮只接索引进度，未改写其增量语义。影响是 symbol/关联工具仍有原来的快照边界，不能把 read/search 内容缓存正确性推广到整个索引。
2. **按同环境历史基线自适应分配单次测试时间尚未实现。** 当前 TestBaseline 有 duration_seconds，但没有足够的 runner/环境与逐 selector 耗时身份；直接按数量比例缩 timeout 会冒充可靠估计。本轮落实的是正式 timeout 来源、既有 closure reserve 和剩余预算 clamp。影响是不会承诺最优验证调度或最短耗时。
3. **需求行为结论保持 unknown。** 原数据没有独立、明确绑定 public contract 与候选的区分性观察记录；自测 PASS 与复现成功不能补出这一事实。已完成三层范围和六维独立投影，未来需要真实的公开契约证据，而不是本轮构造成功结论。
4. **运行兼容与效果证明未完成是本轮明确边界。** 需要另行允许运行测试、受控故障、页面及独立比较。OS 锁只覆盖共享同一系统临时目录的同机进程，不是多主机分布式资源配额；初始串行限制未经吞吐调优。

这些边界明确标为部分实现或待运行验证，不以“完整方案已证明”替代缺失证据。

## 10. 恢复运行验证后的最小顺序

1. 先执行 H/C/B 与前端 W，及既有工具权限、预算、checkpoint/API 回归；核实新工具 schema、取消状态、历史字段。
2. 合成并发/进程退出/磁盘失败：验证锁不重复排队、取消不派发请求、候选/usage 保留、snapshot 冲突拒绝继续、optional projection 可退化。
3. 缓存小/大仓库场景：同长度修改并恢复 mtime、新建/删除/ignore/权限变化、重复旁路和大结果；分开记录身份/查询/搜索时间。
4. 同候选补验和 official-only 恢复；核对 frozen config、timeout、账本与模型派发次数，再启动页面核对 SSE/重连/最终耗时。
5. 最后另行授权少量公开开发案例同条件比较，冷/热分列；不自动进入 test50/test300。证据不足保持 pending，默认版本不自动改变。

> 2026-10-29 运行验证中间状态：详见 [运行验证记录](phase26-runtime-validation.md)。本轮尚未完成，HTTP 总 deadline 与线程/循环风险仍未解决，v4 不代表后续源码修改。

