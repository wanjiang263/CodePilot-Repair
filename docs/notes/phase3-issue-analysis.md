# Phase 3 首批：C-01～C-06（Issue 分析、定位种子与预算上下文）

> 本文保留首批交付时的历史状态。C-07～C-13 后续实现、统一 `docs/03-AGENTS.md`
> 规范引用、Snapshot 契约与当前门禁见 [第二批记录](phase3-localization.md)。

2026-10-12。从 `main`（`1efd8a2`）创建本地分支 `codex/phase3-issue-analysis`，只做本地提交：
不 Push、不建 PR、不合并，也不改动 `codex/l18-smoke-validation`。L-18 真实 API 验收按本轮授权
暂缓，全部 LLM 相关执行只使用 `FakeLLM`；无真实 API Key、无网络、无 Docker、无 GPU。

## 接口与算法决定

- **C-01 数据契约**：`IssueAnalysis` 的权威定义继续在 L0 `schemas/issue.py`，`agents/schemas.py`
  只按 §1.3 的入口重导出同一类型，不复制字段（一条测试直接断言两个模块里的类是同一个对象），
  避免同一契约长出第二份定义。新增的都是 agent 层才需要的配套类型：`RuleEvidence` 是规则抽取的
  中间结果，除了各抽取字段还保留 V-01 解析出的完整 `RetrievalQuery`，让每条证据都能追回原文与
  查询；`IssueSemanticDraft` 只声明 LLM 允许产出的语义字段。集合字段一律 `default_factory`，
  无 traceback、无文件名、只有一句自然语言的 Issue 用空集合与 `None` 表达"没有"，不用
  `bug` / `""` 之类的默认值冒充结论。`TraceFrame` 保持冻结，证据一旦抽出就不许就地改写。
- **C-02 规则抽取**：直接复用 Phase 2 的 V-01（`retrieval.query.build_query`），检索侧与定位侧
  因此共用同一套解析结果，`RuleEvidence.query` 与 `IssueAnalyzer.retrieval_query()` 的返回值相等
  由测试钉住。帧的路径归一、`in_repo` 判定、Windows/Linux 分隔符与大小写差异都沿用 V-01 已有
  实现，不在 agent 层重写第二套正则。`mentioned_symbols` 在 V-01 的标识符之上只剔除 `*.py`
  路径词，路径归 `mentioned_files`，两类证据不混。复现步骤只认两种确定形态：命中小节标题后同
  缩进的列表项（更深缩进算某一步的补充说明、更浅算小节结束），或者编号从 1 起连续递增的有序
  列表；`- 期望 / - 实际` 这类并列项不算步骤，编号不连续的列表（changelog）也不算——把它们当
  复现步骤会让后面的环节读错。`issue_type` 只在词面证据足够时给出，具体类别先于 `bug`（带
  traceback 的"升级后慢了三倍"是 performance），有帧或有成形异常行才判 `bug`，否则返回 `None`
  交给 LLM；规则层不猜。
- **C-03 LLM 语义与证据保护**：`IssueSemanticDraft` 类型层面就没有 `traceback_frames` /
  `mentioned_files` / `mentioned_symbols` / `error_messages` 字段，配置 `extra="ignore"`，模型顺手
  补的同类键在校验阶段即被丢弃；合并时可验证字段一律取规则值，语义字段（类型、摘要、期望 /
  实际行为、补充关键词）取模型值，`reproduction_steps` 规则值优先、缺省才用模型逐字补充的值。
  调用走既有 `complete_structured`，不重复实现 L-09 的 JSON 重试：坏响应重试耗尽抛
  `StructuredOutputError`，provider 异常原样上抛，不返回半成品。`provider=None` 时整个模块是纯规
  则分析器，测试全程用 `FakeLLM` 注入，零真实 Key。
- **C-04 traceback 种子**：`traceback_seeds()` 就是 V-18 的 `pin_traceback(query, symbols)`，返回值
  是 Phase 2 既有表示 `RankedSymbol(pinned=True)`，不新造种子类型。身份匹配规则保持冻结：路径必须
  是仓库内已索引文件、行号必须落在符号区间内、函数名必须与符号名或限定名对得上，第三方路径、
  路径逃逸、未索引文件、越界行号、只有同名函数都不置顶；重复帧合并成一条种子但把每条证据行写进
  `reason`，保留行号与区间便于解释"为什么置顶"。融合算法、分数与排序口径未改动，种子仍是 0 分、
  不参与归一化与融合，只排在普通候选之前（`build_result` 路径有断言）。
- **C-05 Token 计量**：`TokenCount` 把 `tokens`（原始读数）与 `charged`（预算判定口径）分开，估算
  路径的 `charged` 已含余量，精确路径两者相等；`method` / `exact` / `note` 把口径写进结果，估算值
  不会被下游当成真实 token 数。估算按 02 §9.1 的字符数 / 3.5，余量下限 10% 由构造参数校验强制
  （低于 0.10 直接拒绝），且用十进制算：`float` 的 1.1 大于真值，直接乘再向上取整会把 10% 变成
  11%，"留了多少余量"就失去确定答案。只有调用方显式给出 `model` 或 `encoding` 才尝试精确计数，
  默认构造是显式估算模式，不在默认路径上取任何 tokenizer 数据；`CODEPILOT_DISABLE_TIKTOKEN=1`
  强制估算，编码未知 / 依赖缺失 / 编码器抛错都走同一条有记录的降级路径。lone surrogate 记 2 个
  字符（与 UTF-16 宽度一致），偏保守。非法值不静默接受：`margin` 越界或非数值、`budget` 为负 /
  `bool` / 非整数、`text` 非 `str` 全部报错。
- **C-06 ContextBuilder**：`build(purpose, *parts, budget=)` 按 02 §9.1 的八级顺序填充；默认预算表
  照 02 §9.2，键与 `llm.config.KNOWN_ROLES` 对齐（`issue_analysis` 8k、`localizer_files` 16k、
  `localizer_symbols` 24k、`planner` 32k、`coder` 48k、`diagnoser` 24k、`reviewer` 24k）。Phase 3 输入
  的映射写在模块 docstring 而不是藏在代码里：Issue 的结构化摘要、显式文件 / 符号、traceback 与
  错误信息都属 Issue 自带证据进 2 级（traceback 小节排在摘要之前，紧预算下先把最具定位价值的原始
  异常信息留住）；traceback 置顶种子是"目标 symbol"的直接来源进 4 级；其余检索候选签名与文件级
  候选进 8 级，最先被舍弃。优先级是**严格**的：一旦某块内容放不下又裁不小，更低优先级不再尝试，
  因为把碎料塞进剩余缝隙会让"这段代码为什么没进上下文"变成无法回答的问题（隔离与重复判定不封住
  后续内容，那两类不是预算问题）。裁剪可解释：Issue 原文按文档保留首尾、标注中间省略的行数，代码
  片段保留头部并标注省略行数，标题里的文件路径与行号区间不参与裁剪，所以截断后身份仍可追。去重按
  `dedup_key`（symbol_id 或路径区间）或内容哈希。预算是硬约束：分片按**渲染后**整体计数，装配完再
  用同一计数器复核成品全文，仍超限就从最低优先级撤（记 `assembled_overflow`）；`Context` 的类型校验
  要求 `used <= budget` 且 `remaining == budget - used`，越界结果连构造都过不去。隔离与信息边界：
  Gold Patch / 评估标签 / 隐藏测试按标记拒绝并报告命中的**最具体**标记；`source` 里的绝对路径与
  `..` 逃逸被挡下，给了 `workspace_root` 还要解析一次确认没逃出根；检索内容一律渲染进数据节，节首
  声明它只作证据、其中的"指令"不是指令，Builder 本身不执行任何内容。元数据输出 `included` /
  `omitted` 逐条级别、标签、source、占用、字符数、是否截断与舍弃原因，外加计数口径与降级原因。
- **信息隔离**按 03 §11.2 的硬规则落进 Builder：拒绝标记覆盖 gold / solution / reference_patch /
  expected_patch / `fix.patch` / `patch.diff` / hidden test / eval label / retrieval_gold /
  benchmark answer / `fail_to_pass` / `pass_to_pass` / `hints_text` / base 之后的提交；同时命中多个
  标记时报告最具体的那个，审计才看得出是被哪条规则挡下的。

## 跨簇调用登记（§1.3 例外）

`agents/`（L3）需要调用同层的 `retrieval/`（L3）。按 §0 规则 3 先在冻结清单里登记，而不是绕过约束：

- `codepilot.retrieval.query.build_query(issue, *, repo_files, repo_root) -> RetrievalQuery`：C-02 的
  V-01 解析复用，也保证检索与定位看到同一套证据。
- `codepilot.retrieval.fusion.pin_traceback(query, symbols) -> list[RankedSymbol]`：C-04 的 V-18 置顶。

两个调用都是只读纯函数，不引入新的状态或副作用；`context/` 不 import `retrieval/` 或 `agents/`，
只吃 L0 的 `IssueAnalysis` / `RetrievalResult` 结构与调用方给的 `CodeSnippet`。

## 验收

- 新增 512 项：`tests/unit/agents/` 77、`tests/unit/context/` 428（含 351 项预算不变量矩阵）、
  `tests/integration/test_phase3_issue_context_pipeline.py` 7。
- 定向：`pytest tests/unit/agents tests/unit/context tests/integration/test_phase3_issue_context_pipeline.py`
  → 512 passed，exit 0（本地按组各跑一次，最后一组为 505 + 7）。
- 全量离线：`pytest -m "not docker and not llm" --cov --cov-report=term-missing` →
  1673 passed / 0 failed / 1 skipped / 1 deselected，覆盖率 95.39%（门禁 80%），exit 0，耗时 642s。
  唯一 skip 是既有的 `CODEPILOT_TEST_EMBEDDING_MODEL` 未设置项。
- 新模块覆盖率：`agents/schemas.py` 100%、`agents/issue_analyzer.py` 96%、`context/tokens.py` 100%、
  `context/builder.py` 98%。剩余未覆盖的是符号链接逃逸的 `escapes_root` 分支（需要真实 symlink，
  Windows 上不可靠）与几处 if-false 分支（无 plan / 无 retrieval / 无候选时的旁路已被其他用例走过）。
- `ruff check .` exit 0；`ruff format --check .` exit 0；`mypy src` exit 0（93 个源文件，strict）。
- 集成链路逐项证明：规则证据不被 FakeLLM 覆盖（`guessed.py` / 编造异常 / 编造符号全部不出现）；
  仓库内 traceback 种子与检索结果按 `symbol_id` 关联且 `pinned` 排第一、0 分；ContextBuilder 用的是
  `retrieve_symbols` 的真实 `RetrievalResult`（`fused_symbols` / `symbols` / `files` / `snapshot_id`）；
  预算阶梯上 `used <= budget` 且保留内容只增不减；引用路径、行号区间与 `symbol_id` 在成品文本里
  保持正确；两次完整运行（分析 + 检索 + 上下文）逐字节一致；Gold / 隐藏测试 / workspace 外路径被
  拒绝；链路对工作树只读（`git status --porcelain` 为空）。

## 限制与 NOT RUN

- 全部结论来自合成 Issue、`FakeLLM` 与 `mini_calc` fixture 的**离线工程正确性**，不构成模型效果或
  定位准确率提升的结论。
- L-18 真实 API Smoke：NOT RUN（本轮授权暂缓）。C-14 真实 LLM 定位评估：NOT RUN。
- 估算口径（字符 / 3.5 + 10%）在 CJK 文本上偏乐观，`Context.used` 是"按采用口径不超预算"，不是对
  某个真实模型 tokenizer 上限的验证；精确口径需要显式 `model` / `encoding` 且本地 tokenizer 可用。
- tiktoken 若本地无 BPE 缓存会自行获取它自己的 tokenizer 数据文件（非依赖安装、非模型 API 调用）；
  离线环境用 `CODEPILOT_DISABLE_TIKTOKEN=1` 或注入 `loader` 强制估算。本批所有测试都注入 loader 或
  走显式估算路径，不触网。
- C-07（`ContextTooLongError` 半预算重建）、C-08（文件骨架）、C-09～C-14（分层定位与评估）未开始；
  本轮没有提前实现 C-09 的文件级 rerank 或 C-10 的 symbol 级定位。
