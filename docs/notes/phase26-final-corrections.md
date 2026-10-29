# Phase26 最终修正与计划补齐（v4）

日期：2026-10-29。HEAD：`07625a83f7d39db3f88ddde4ebd5005851978afa`。本文是最新状态；v3 报告、静态记录与候选保留为历史事实。

## 1. 完成结论

三处缺陷已修正；远程模型已移除无条件全局单槽；索引关联闭包和同身份验证耗时分配已进入真实调用链。完成的是工程实现与静态交付。所有速度、吞吐、修复质量、恢复兼容、增量/全量等价性及交互效果均 **待验证**。

本轮未运行测试、应用、构建、Docker、Harness、服务商 API、模型或 RSI 实验；未安装插件，未启用 Superpowers，未委派 agent；没有 commit、push、PR。当前工作树和既有未提交改动保留。适用规则为用户提供的 AGENTS.md。

## 2. 三处缺陷：根因、修正与接入

|缺陷|根因|修改及真实入口|测试（未执行）|
|---|---|---|---|
|范围目录被拒绝|resolve 默认 expect_file=True|新增 `WorkspacePathPolicy.resolve_directory`，内部显式 expect_file=False；QueryCache 与 `_search_root` 共同使用。目录、根、deny、越界、目录链 symlink 都检查；glob 仍由原 rg/Python 引擎执行，不把目录解析当 glob 验证。无安全前缀回退原根搜索，缓存例外仍由 registry 旁路并报告|F：directory_prefix、directory_symlink、scope_registry；原 H/C 缓存测试|
|历史源文件存在时无法读取|must_exist=False 是创建新文件语义，拒绝已有文件|`read_evidence` → `check_historical_read`，按当前存在状态调用原 resolve，再检查实际解析目标的 deny scope。保留原创建语义；缺失源文件仍做边界和权限校验；历史分页、当前任务根、内容哈希、16MiB 上限保留|F：historical_existing_and_deleted_permissions、tamper_and_other_task|
|排队后旧 timeout 继续生效|等待前算 timeout，取得资源后没有重算|统一 `observability/dispatch.py`；AccountingSandbox 两个资源均取得后，重新校验取消/准入并 clamp；provider limiter 与费用锁等待也检查同一 deadline，HTTP 真正派发前重新算各阶段 timeout|F：wait_reclamps、expired_or_cancelled、budget_rejection、known_scope_does_not_fit|

## 3. 资源策略、deadline 与副作用边界

- `Deps.dispatch_deadline` 从既有 started_at/max_runtime_seconds 的剩余预算锚定单调时钟；同进程仅收紧，不重置预算。恢复依旧计入既有已消耗墙钟。进程内排队和执行用 monotonic。
- `builder._guard` 将原同步 I/O 节点放入工作线程的独立循环，外层事件循环不承担阻塞锁。取消外层协程时设置线程停止标记并等待在途节点收尾，不遗弃仍执行副作用的线程。资源重入身份含线程和实际 asyncio task，不能用另一个 task 继承的上下文冒充持有者。直接从其他事件循环同步调用工具时，仅允许立即取得空闲槽；出现竞争会明确拒绝并要求移到 worker，避免阻塞调用方循环。
- 本地重型索引/构建和测试共用同机保守单槽 `pressure`；workspace 使用规范化实际路径的独立锁。锁顺序为 pressure → 按路径排序的 workspace；逆序立即拒绝，异常/取消释放已取得资源。OS 锁由内核在退出时释放；文件仅标识槽，不用 PID 文件猜占用。没有集群调度服务。
- 远程模型只使用既有 `ProviderSpec.max_concurrency` 与 Registry 的 provider limiter；该配置的作用域沿用原 Registry/进程，跨进程上界仍受既有 worker 配置控制，不另造机器级模型槽。没有把每个模型请求无条件压成单槽。启用既有共享费用派发账本时，原账本仍在其安全临界区串行保护派发/结算，未改变其授权上限或冻结规则。
- limiter 每 0.1 秒轮询取消/准入；拿到资源后再次检查。测试 timeout ≤ 调用者/runner 上限、任务剩余时间减闭环预留、估算建议值。模型 HTTP 的 connect/read/write/pool 各自不超过原 provider/client 上限与当前剩余时间。
- 测试次数在最后检查之后、inner.run_tests 调用之前增加。模型 HTTP attempts 在最后检查之后增加；排队或费用准入拒绝不写 usage unknown。若持久化预留后、HTTP 前被取消/耗尽 deadline，新预留释放，原 admission 审计保留在同一账本的 cancelled_admissions，requests 不增加；已有历史请求不改写。真正已发出的调用仍按原未知费用规则处理。
- 最后检查与实际函数/网络调用之间仍存在不可消除的微小竞态；已越过派发边界的操作按已派发处理，不伪称强制终止或免费。排队、取得资源、执行释放分别有事件。0.1 秒是轮询设计，并非测得的取消延迟。

## 4. 两项剩余计划的实现与回退

**索引关联闭包。** 原库按文件保存内容哈希/FileNode、SymbolRow，跨文件 IMPORT/CALL/REFERENCE/INHERIT/TEST 保存在 edges；旧边不足以还原所有未解析导入、别名和包再导出。因此采用明确的完整关联闭包回退：有文件变化时重建全体关联，保证新增、删除、重命名、原未解析名字、新目标、循环导入和未变化测试的关联都重新计算；旧符号、旧边一起在调用方事务中替换。这里不声称实现了最小逆依赖集合。

`Indexer` 内有内容键控、实例级有界解析树复用（128 项、8MiB 原源码载荷，单文件 1MiB；解析树对象有额外内存开销）。同一 Indexer 的未变化树可复用；冷进程、逐出或缺失树会重新解析以恢复绑定，明确回退，不保留已知旧边。扫描不可读或扫描后内容变化会抛错，保留先前已提交索引，不将未知当删除。进度区分实际解析、树复用与关联源数量。

新增最小派生表 `index_semantics`，由已有 init_db/create_all 为旧数据库补表，无历史事实迁移或第二索引库。指纹含解析/关联源码、parser/scanner 类型和 tree-sitter 版本。内容和语义均一致时 `verified_snapshot_v2` 直接复用；旧索引缺少标记、语义变化或源内容变化时 `complete_closure_v2` 重建，标记与索引在同一事务提交。assembly 对 seed 和 checkpoint 索引均走此正式入口，准备 artifact 记录实际模式。实际等价性与成本未运行验证。

**自适应时间分配。** `AccountingSandbox.run_tests` 是目标测试、基线、回归与 Coder 工具的统一入口。身份包含 repository/base、image_id/cache_key、runner 类型/适配器、EnvSpec 配置（除 timeout）及有序 selectors。执行耗时、收集顺序、状态、完整性、冷环境/准备耗时和估算决策进入 ExecutionResult.diagnostics；原执行目录新增只写一次的 dispatch-timing.json。恢复仅读取当前任务的有界执行事实，旧 baseline 没有身份时不凭 duration 猜匹配。

精确范围完整样本用中位数加 25% 与 5 秒余量，并不低于已观察到的必要最大耗时。TIMEOUT 只当下界；非有限/非正/无法转换数据拒用并计数；溢出建议值饱和在上限之外。只有显式节点集合与实际集合一致才标记范围完整；文件/全仓执行保留为低置信度聚合数据，不能据此降低默认上限，不能从 PASS 推导全部范围完成。启动开销未独立测量时标 unknown，保留总执行耗时和独立准备耗时，不伪造逐 selector 分配。

所有建议最终受同一 deadline/Review 预留和显式 runner cap 限制。既有同候选有效证据复用先于派发；有明确完整/超时证据且已知范围放不进剩余窗口时，记录 validation_deferred 与完整 selectors，停止派发，保留 incomplete，而非偷偷缩范围或扩大总预算。没有可靠证据时沿用原保守默认及剩余预算 clamp。默认先必要验证、再扩大回归的顺序不变。估算命中率与净收益均待验证。

## 5. 全量覆盖表

H/C/B/W 含义沿用 v3；F=`tests/unit/test_phase26_final.py`；I=`tests/unit/repo/test_index.py`。下表状态为源码实现状态；所有测试未执行。详细静态结果以 JSON 为准。

|计划条目|当前实际实现|真实调用入口|尚缺内容/边界|本轮处理方式|对应测试代码|静态验收|运行验证|
|---|---|---|---|---|---|---|---|
|§1 六目标|保持六目标与既有成功定义|现有任务/报告链|没有效果样本|不修改预算、结果与费用|H/C/B/W/F/I|源码/文档审查|待验证|
|§2 事实与直接缺口|官方 verdict/身份共同决定终态|m3_batch._evaluate_official|真实官方恢复未运行|复用上一轮补丁并审查|B|Ruff/mypy/diff|未执行|
|§3 研究取舍|小型内部机制|原 agents / graph / tools|没有论文收益迁移结论|ECC 仅机制借鉴|—|来源阅读|待验证|
|§4.1 共享状态|RepairState 权威；默认字段兼容|guard → checkpoint|无 exactly-once 保证|节点提交；缓存/投影仅派生|H/C|AST/类型检查|未执行|
|§4.2 插件边界|顺序只读 Observer；异常隔离|Lifecycle → registry/tracking|不是动态插件市场|保持现有协议，不加总线|H|Ruff/mypy|未执行|
|§4.3 快照与切换|新任务绑定，恢复核验 schema/hash；显式父版本选择|begin / RSI select/rollback|没有任意 Python 热替换|受控 worker 重启沿用 checkpoint；不在途换策略|H/C|类型/调用链|未执行|
|§5.1 Git/索引/环境/响应复用|复用原缓存与身份校验|assembly / stage_recovery / sandbox / provider|内容+语义一致复用；变化采用完整关联闭包回退|Indexer + index_semantics；准备来源投影、正式 official-only 恢复|H/B|调用链审查|未执行|
|§5.2 搜索/读取去重|内容身份、权限、范围、字节上界、旁路、区间标注|registry → QueryCache/read_tools|全仓查询没有缓存命中承诺|直接原路径；有前缀才扫描范围；昂贵范围不重复扫描|H/C|AST/Ruff/mypy|未执行|
|§5.3 资源控制|同机 OS 锁 + 线程门禁，真正阻止并发派发|assembly index/build、AccountingSandbox、tracking、write tool/reset|不是分布式集群限流；吞吐最佳值未知|重型/测试共享压力槽；模型复用 provider 配额；workspace 互斥；统一 deadline|C|平台分支/类型审查|未执行|
|§6.1 四项契约|change/preserve/interface/discriminator|Planner → Coder / Reviewer|独立行为证明缺失时 unknown|保持来源与 explicit/inferred/unknown|H|schema/调用链|未执行|
|§6.2 探索重定向|缺口、证据、选项及一次诊断返回|Coder 工具循环|相关性是计划文件重叠启发式|实际 diff 变化与存在分离；诊断不扩额度|H/C|AST/类型检查|未执行|
|§6.3 失败与阶段摘要|候选绑定、范围缺失、基线关系、引用|evidence → _run_coder → ContextBuilder|无明确证据不编造排除假设|自然边界确定性重建；上下文预算内替换|H/C|类型/引用审查|未执行|
|§7.1 三层证据|需求行为、受影响目标范围、扩大验证范围|verification_view → API / UI details|没有独立绑定需求观察|未知保留；真实 requested/observed/missing 分列|H/C|类型检查|未执行|
|§7.2 六维独立语义|execution/behavior/regression/review/coverage/official|同一证据投影|PASS 不等于 behavior 满足|保持 acceptance 兼容；stale/skip/missing 不冒充 PASS|H|调用链审查|未执行|
|§7.3 验证时间|同身份耗时估算 + closure reserve + 派发后限时|nodes/tools → AccountingSandbox → Docker|旧数据无身份时回退；不伪造逐 selector 耗时|AccountingSandbox → validation_time → dispatch → runner；范围不缩减|H|类型/预算链审查|未执行|
|§8.1 官方与 runner 可靠性|有效判分保留、无效保持可恢复、timeout 来源|m3_batch / official.effective_env_spec|外部 Harness 实效未知|不重复内部模型派发|B/H|Ruff/mypy|未执行|
|§8.2 恢复决策|checkpoint、阶段导入、候选/证据身份、unknown 费用原规则|worker / checkpoint / stage_recovery|副作用完成而 checkpoint 未落盘窗口仍在|保留旧证据；取消等待不抹 usage/候选|H/C/B|调用链审查|未执行|
|§9 阶段体验|克隆前开始事件、索引计数、阶段耗时、候选、取消、策略版本|assembly → EventSink → API/SSE → HarnessStatus|展开区显示期望/当前/下次策略；无在线编辑|统一投影、memoized 合并；无 ETA|C/W|TS/ESLint|未执行|
|§10 生命周期|task/model/tool/candidate/validation/cancel/resume/end；同步调用结束 checkpoint_saved|现有真实入口|checkpoint_saved 仅声明完成的同步图调用|观察故障降级到日志，不阻断核心|H/C|静态调用链|未执行|
|§11.1 RSI 范围|离线档案，不运行模型/评测|RSI CLI|没有净收益结论|新候选 pending|H/C|导入边界审查|未执行|
|§11.2 L0/L1/L2/L3|L0 来自状态；L1 仓库绑定；L2 通用；L3 校验比较引用|retrieve → begin → Coder context|没有真实比较条目|条件执行、拒用原因、总长度上限|H/C|AST/类型检查|未执行|
|§11.3 候选流程|父子档案、配置来源、代码/提示快照、比较导入、readiness|archive/import-comparison/promotion-readiness|真实独立比较与人工提升决定缺失|不自动变更 validated 或 active|H/C|格式/哈希核对|未执行|
|§11.4 回放归因回滚|trace/decision；父版本选择；源代码单独归档|RSI CLI|决策回放不能推出模型效果|比较固定模型/provider/预算/环境/成功定义/任务集；五类指标分开|H/C|类型/路径/哈希检查|未执行|
|§11.5 污染防护|public_only；拒绝隐藏评测经验；不改成功定义|validate_snapshot/validate_comparison|真实性仍需审核原证据|有 hash 也不自动视为已证明有效|H/C|静态审查|未执行|
|§12 A–E 实施批次|上述实现已进入真实调用链|表中各入口|本轮 A–E 已接入；实现边界见第 10 节|复用已有能力，不以事件冒充调度|H/C/B/W/F/I|见检查 JSON|未执行|
|§13 最小运行验证|已给出次序|第 10 节|运行授权不在本轮|仅编写，不运行|测试清单|文档核对|未执行|
|§14–15 历史/状态|保留编制与上轮记录，明确本轮状态|plan / implementation / 本报告|无效果结果|新候选另存，旧档案保留|—|一致性核对|待验证|


补充条目：A 的目录/历史证据权限由工具真实入口消费；B/C 的 deadline 在 graph → AccountingSandbox/provider limiter → runner/HTTP 派发链消费；D 的语义表由 Indexer 与 assembly 消费；E 的估算直接修改实际 EnvSpec timeout，并写执行诊断。没有只定义而未调用的最终方案组件。

## 6. 静态检查

精确 argv、范围和实际输出在 phase26-static-checks.json；源码白名单在 phase26-final-source-manifest.json。命令仅 AST/JSON 解析、Ruff check、ruff format --check、mypy --follow-imports=silent --no-incremental、TypeScript --noEmit、ESLint --max-warnings=0、Git diff --check。不导入应用、不运行测试收集。

本轮已修正实际诊断：PowerShell 不支持 Bash 路径花括号展开，改用明确路径；Deadline 返回类型从 Any 改为 Deadline；asyncio.run 入口显式包装 Coroutine；HTTP timeout 改为库接受的关键字构造并保留各阶段上限；Ruff 导入/格式/未使用名问题已修正。测试对照调用改用已有 dump_index(stores, repository_id)，旧“过期关联是预期”的断言改为完整关联不变量；这些断言未执行。

最终实际结果：47 个 Python 文件通过 AST、Ruff check、format check；42 个生产 Python 文件/批次脚本通过 mypy；5 个前端源码/测试文件通过 ESLint，TypeScript noEmit 与 Git diff whitespace 通过，所有退出码为 0。2 个 locale JSON 成功解析。五版档案的 202 项源码哈希通过；v4 共 54 个源码文件与当前工作树的规范化内容及原始字节一致，snapshot/diff 哈希一致。候选 diff 为 298796 字节，`git apply --numstat` 成功解析 54 个文件记录（未应用补丁）。

上述检查没有收集或执行任何测试。18 个新增最终修正测试函数和 2 个新增索引对照函数（6 个展开场景）只完成编写；原测试修正也未执行。

## 7. 测试代码（全部未执行）

- F：目录/根/缺失目录/越界/symlink；实际 registry 范围缓存、负结果/内容/ignore 失效；历史文件存在/删除/deny/篡改/其他任务/分页。
- F：等待后 timeout 收紧、耗尽不调用 inner、取消不增加次数、同 workspace 重入/异常释放、独立 workspace、进程退出释放、两个独立 provider 槽、全局费用拒绝不计 unknown。
- F：精确范围/环境/runner/顺序身份、超时下界、无数据/无效值/极端值、Review 预留、已知范围放不下不派发、真实派发诊断记录最终 timeout。
- I：修改/新增/删除/别名/循环、旧未解析导入、未变化测试 medium link、重复目标删减后的 confidence、无关解析树复用、规范化增量与全量节点/边比较、旧语义失效。原已存在的相关测试同步更新，不运行。
- 原 H/C/B/W 保留；C 的 guard 测试补齐新的 deadline 依赖。以上不能替代运行验收。

## 8. 使用方法

无需新服务。现有 CLI/API 入口自动使用权限修正、资源策略、索引校验与时间分配。远程并发读取原 provider.max_concurrency 与 worker 配置，不修改模型、服务商和费用授权。workspace 仍各自互斥；本机压力初始单槽的最佳值未验证。

恢复继续原 checkpoint 和 official-only 入口；不会因为策略指针变化而重新派发已完成模型。准备详情查看 association_mode/parsed_files/parse_reused_files；执行 JSON 的 diagnostics.time_allocation 显示来源、置信度、建议值、输入 cap、总剩余、预留和实际 timeout，timing_fact 显示真实观测。模型并发取消及预算停止通过原事件/状态通道呈现。索引旧表无需手工删库：init_db 补语义表，首次正式索引事务重建；不要直接把未校验旧 DB 当完整索引。已有 RSI 离线回放/比较/父版本回滚使用方法不变，不在本轮运行这些入口。

## 9. 候选档案

新候选 `configs/harness/candidates/phase26-candidate-v4`，父版本 v3；v1/v2/v3 与 baseline 原样保留。sources 包含源码白名单的完整当前内容，candidate.diff 包含 HEAD 到当前的 tracked 和新增源码差异，排除档案自身、大型历史日志与文档，防止递归。规范化 UTF-8/LF 源码哈希、工作树原始字节哈希、规范 JSON snapshot 哈希、diff 字节哈希分开校验。

代码回滚仍需匹配源码 checkout/worker，策略父版本选择只影响未来提示/经验。v4 为 pending；没有 active 指针、validated 升级、真实比较结果或发布。源码与档案核对结果见静态 JSON。

## 10. 剩余限制与待运行清单

- 本轮实现完整关联闭包的保守回退，未实现最小持久化逆依赖集合；冷变更索引可能重新解析全体 Python 文件。语义一致且内容未变可复用已提交索引，warm Indexer 可复用解析树。内存与速度取舍待测，不宣称提速。
- 时间事实仅复用当前任务/恢复执行 artifact；旧无身份数据、缺少逐项时间、未知启动成本不被补造。文件/全仓只有聚合数据时不据此缩 timeout。预算不足保持未完成，不改成功定义。
- 本地锁只协调共享同一系统临时目录的同机进程；provider 配额是既有 Registry/进程作用域，共享费用账本保留原串行安全边界。无跨机器最优调度保证。
- 新增字段有默认值，旧 ExecutionResult/checkpoint 无 timing 数据可回退；真实旧快照恢复、线程切换、SQLite/驱动兼容、取消边界均待运行验证。
- 最小验证次序（另行授权）：F/I 与原 H/C/B/W → 跨进程锁/线程取消/费用门禁故障注入 → 小仓增量/全量对照与旧索引升级 → 有限真实 runner 的同身份计时/预算不足 → 原同候选/official-only 恢复与 UI → 最后同条件小样本性能比较。不自动进入 test50/test300 或 RSI 实验。

> 2026-10-29 运行验证中间状态：详见 [运行验证记录](phase26-runtime-validation.md)。本轮尚未完成，HTTP 总 deadline 与线程/循环风险仍未解决，v4 不代表后续源码修改。

