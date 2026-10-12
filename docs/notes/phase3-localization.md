# Phase 3 第二批：C-07～C-13

2026-10-12，直接在 `codex/phase3-issue-analysis` 的 `8824185` 上继续开发。
项目流程规范为 [docs/03-AGENTS.md](../03-AGENTS.md)，仓库根没有另一个项目规范文件；
当前会话的全局 AGENTS 指令另行生效。没有从旧 main 重建，没有 Push、PR 或合并。

## 接口与证据边界

- `ContextBuilder.complete_structured(provider, purpose, *parts, schema=, budget=,
  diagnostics=, required_group=)` 是兼容协调层，不改变 `LLMProvider.complete/close`。
  首次明确的 `ContextTooLongError` 才用 **原预算 // 2** 重建；预算 0 无法缩小则直接抛出，
  奇数向下取整。每次都用原始 `ContextPart` 与已有 `build()`；全调用最多一次重建，
  包括 JSON 校验重试中发生超限的情况。第二次超限原异常上抛。网络、鉴权、限流与
  `StructuredOutputError` 不进入减半逻辑。
- 复用 `llm.structured.complete_structured` 的三次结构化尝试。校验反馈也经 ContextBuilder
  装配，避免校验重试的历史文本绕过预算；没有完整 system/schema 或没有实际装入候选时抛
  `ContextBudgetError`。诊断包含原预算、重建预算、重试次数、原因，以及每次 Context 的
  计数策略、取舍与实际占用，失败时仍可由调用方读取。估算沿用字符 / 3.5 + 10%，**不是
  对真实模型 token 上限的证明**。
- `SkeletonBuilder(root, RepositoryIndex).build(path) -> FileSkeleton` 直接复用 R-07
  的签名、装饰器、qualified name、父作用域与闭区间行号。不执行源码，不 import 被分析模块，
  不新建 AST/索引体系。读文件前使用共用 `workspace_source_path()`；此函数是既有 Phase 2
  路径校验的提取，`retrieval.documents.source_path()` 保留原异常与测试注入契约。
  读取后核对索引 SHA-256，过期索引、未索引文件、读/编码错误明确失败；空文件为空定义，
  语法错误文件显示无可信定义状态。骨架正文不包含实现、docstring、函数体注释。
- `Localizer(provider, index, workspace_root, snapshots, builder=, file_budget=16000,
  symbol_budget=24000).localize(analysis, retrieval) -> LocalizationResult`。
  输入是现有 IssueAnalysis 与 Phase 2 RetrievalResult。文件按冻结排名取前 15，
  包含原分数、pinned 与检索来源，模型只用索引 File ID 选择最多 3 个文件。
  Symbol 在选中文件内取完整融合排名的前 10 个真实定义（无融合排名时兼容 symbols），
  使用已有 Symbol ID，最多选 5 个。重复选择保留第一次，按模型输出顺序稳定截断。
  原始候选块原子装入，预算删掉的候选不能被模型选中；无候选/合法空选择显式返回空定位，
  未知身份、错误文件、索引与检索范围不一致、虚构范围均抛 `LocalizationError`。
- 源码每个文件读一次，嵌套定义/类方法的重叠范围先合并，再逐行编号装入 ContextBuilder。
  模型建议范围必须属于实际 Symbol 闭区间，并受实际文件行数约束。最终 `symbols` 保存完整
  索引范围；`symbol_decisions` 保存模型建议子范围，二者明确分开。源码、骨架与提示词注入
  文本都作为不可信数据，模型没有工具调用或仓库写入能力。隔离路径在骨架读取之前拒绝。
- `agents.schemas.LocalizationResult` 重导出同一个 L0 Schema；没有平行 RankedFile /
  RankedSymbol。`files` 与 `symbols` 是原检索项的深拷贝，**reason 仍为原始检索证据**，
  不用模型理由替换 pinned 依据；每个选中身份必有对应 `file_decisions` / `symbol_decisions`
  的非空定位理由与步骤标记。Schema 强制 ≤3/≤5、身份去重、文件归属、范围与理由约束。

## Retrieval Snapshot 契约

`RetrievalSnapshotStore(ArtifactStore).save(result) -> retrieval-sha256-<64 hex>`；
`load(id) -> RetrievalResult`；`path_for(id)` 解析当前 run 内的真实路径。

- 保存路径：`artifacts/runs/<run_id>/retrieval_snapshots/<id>.json`。
- ID 是版本化规范 JSON（`version=1`、完整 `retrieval`）的 SHA-256；包括原始 upstream
  `snapshot_id`，不改写 Phase 2 原对象。保留所有通道、融合前/后候选、分数、排名、权重、
  rrf_k、query 与 pinned 依据。相同输入得到同一 ID 与逐字节相同内容。
- 同目录临时文件 → flush/fsync → 原子 replace → Schema/hash 读回校验，成功后才返回 ID。
  失败不报告成功，临时文件清理；已有内容损坏不会静默覆盖。缺失、损坏、非法 ID 明确抛
  `SnapshotError`。既有 ArtifactStore 的其他写入方法不变，无新增数据库/服务。
- 定位结果的 ID 可解析到本次完整 RetrievalResult；非空定位不接受缺少 Snapshot 的状态。
  不存入 gold patch、隐藏测试、benchmark 标签或真实模型效果指标。

## 测试与门禁

新增 **82 项**：五个新增测试文件 78 项，已有预算不变量文件扩展 4 项；没有复制原 351 项矩阵。

| 范围 | 已验证用例 | 核心断言 |
|---|---:|---|
| C-07 `test_rebuild.py` | 13 | 一次减半、错误分流、奇数/0/极小预算、校验重试超限、证据隔离与去重 |
| C-08 `test_skeleton.py` | 10 | 多行/async/装饰器/嵌套/继承、实现不泄漏、编码/过期/路径/真实 symlink |
| C-09/C-10 `test_localizer.py` | 32 | 真实候选、数量裁剪、ID/行范围、可见候选、pinned、预算与源码去重 |
| C-11 `test_localization_snapshot.py` | 17 | JSON round-trip、结果约束、稳定快照 ID、篡改/缺失/原子写入失败 |
| C-12 `test_localization.py` | 6 | 真实检索、完整链路、两次输出/快照/请求逐字节相同、源码只读与非法输出 |
| C-13 `test_budget_invariant.py` | 355（含原 351） | 大量实际候选/源码、16k/24k/半预算、超长 Issue、Unicode 估算、真实 symlink |

定向命令（虚拟环境 Python）：

```text
pytest tests/unit/agents/test_localizer.py tests/unit/agents/test_localization_snapshot.py tests/integration/test_localization.py -q
  38 passed，exit 0（随后扩展结果约束与安全用例）
pytest tests/unit/agents/test_localizer.py tests/unit/agents/test_localization_snapshot.py tests/unit/context/test_rebuild.py tests/unit/context/test_skeleton.py tests/unit/context/test_budget_invariant.py tests/integration/test_localization.py --cov=codepilot --cov-report=term-missing --cov-fail-under=0 -q
  429 passed，exit 0，147.72s
pytest tests/unit/context/test_rebuild.py -q
  13 passed，exit 0，1.43s（最后增加 4 项错误分流/再次超限；其中原 9 项复查）
```

定向覆盖率只用于看新模块，不能作为全仓库门禁：Localizer 94%、Skeleton 97%、
LocalizationResult 100%；仅跑相关用例时全仓库覆盖率 55%，所以该命令明确设定
`--cov-fail-under=0`。完整离线门禁保留配置中的 **80% fail_under**，没有降低标准。
实际 433 个相关用例由 429 项与最后 4 项增量组成，最后的完整离线运行再次统一覆盖。

Windows 沙箱中的 pytest 临时目录访问最初出现 WinError 5，改为获准的本机测试执行，
没有修改测试断言或过滤警告。长 Unicode 参数自动生成的测试 ID 超过 Windows 环境变量
32767 字符限制，设置短 ID 后保留相同原始长输入重跑通过；没有将未运行用例记为 PASS。
先行失败测试和开发中发现的错误已经修复，最终结果以门禁日志为准。

最终完整离线门禁只运行一次，证据在 `artifacts/phase3-localization/`：

| 命令（虚拟环境 Python `-m`） | 实测结果 | 退出码 | 原始证据 |
|---|---|---:|---|
| `ruff check .` | PASS | 0 | `ruff.log` / `ruff.exit` |
| `ruff format --check .` | PASS，186 files already formatted | 0 | `format.log` / `format.exit` |
| `mypy src` | strict PASS，95 source files | 0 | `mypy.log` / `mypy.exit` |
| `pytest -m "not docker and not llm" --cov --cov-report=term-missing --cov-report=json:artifacts/phase3-localization/coverage.json` | **1755 passed / 0 failed / 1 skipped / 1 deselected**，949.44s；**95.50%**，保留 80% 门禁 | 0 | `pytest.log` / `pytest.exit` / `coverage.json` |

唯一 skip：`test_embeddings.py` 的预安装真实代码 Embedding 模型未设置
`CODEPILOT_TEST_EMBEDDING_MODEL`，NOT RUN。llm 用例由 marker 排除；未执行产品 Docker
Sandbox，没有真实 API 或付费 Embedding 调用。完整测试没有 warning 汇总。
新链路模块的完整覆盖率（coverage.json）：Localizer 93.81%、Skeleton 97.01%、
ContextBuilder 98.82%、LocalizationResult 100%、TokenCounter 100%、ArtifactStore/Snapshot 98.57%。
预算不变量、定位范围、Snapshot 可追溯、两次运行确定性和真实 symlink 均由上述实跑用例断言。

本地提交：`d06cb17`（C-07/C-08），`03d9b23`（C-09～C-11）；C-12/C-13 的集成、
安全补测及本记录合并为最后一笔本地提交。没有尚未修复的实际失败或 BLOCKED 项，
未验收范围见下表；未据 FakeLLM 测试报告真实模型准确率。

## 历史遗留问题处理

| 问题 | 状态 | 处理与边界 |
|---|---|---|
| 中文/Unicode/多语言估算与超限恢复 | MITIGATED | 保留字符 / 3.5；新增密集 CJK、emoji/组合字符、多语混合预算边界与一次减半恢复/抛错测试。真实 tokenizer/模型上限未验收 |
| 真实 symlink 逃逸 | FIXED（本地验收缺口） | Skeleton 在读前拒绝 link；ContextBuilder 解析后拒绝仓库外目标。Windows 本轮实际可创建 symlink，两个真实逃逸用例已运行，无模拟 PASS；Linux CI 仍待验收 |
| 规范文档路径 | FIXED | 当前项目规范统一引用 `docs/03-AGENTS.md`；没有复制/重写整份规范 |
| 首批 Commit Scope 文字错误 | MITIGATED | 保留用户登记的历史 Scope 文字错误；按首批任务和实际文件解释归属。本轮只新建提交，不 amend/rebase/reset，不改写历史 |
| 远端 Linux CI、L-18、C-14、真实问题 Benchmark | DEFERRED | 按本轮授权保持 NOT RUN；不 Push，不使用真实 API/付费 embedding/GPU，不启动或开发产品 Docker Sandbox |

离线 Localization 链路可供下一阶段消费；Phase 3 的真实模型整阶段验收仍未完成，
FakeLLM 正确性不代表定位准确率或效果提升。没有提前实现 Planner/Coder/PatchValidator。
