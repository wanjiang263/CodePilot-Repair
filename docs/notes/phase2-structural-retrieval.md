# Phase 2 第三批：V-11～V-13（结构 / 测试邻近 / 历史通道）

## 范围与 PR 收口

PR #3 的最终 head `3672599` 与上一批确认的审查版本一致：CI 六项检查成功、`mergeable=MERGEABLE`、`mergeStateStatus=CLEAN`、无正式审查请求也未留下未解决的审查线程（`reviewThreads` 为空、行内评论 0 条）。本批先复核了新增业务代码（`embeddings.py`、`semantic.py`、`service.py` 与 CI 变更），未发现影响正确性、兼容性或安全性的新问题：批量 API 的 index 校验拒绝重复/越界/非整数、缓存条目按 SHA-256 内容寻址且 `allow_pickle=False`、FAISS 结果非法时降级 NumPy 并发出 RuntimeWarning。本地相邻测试 120 passed / 2 skipped 复现通过，因此按合并条件直接合并，未重复审查或重复验收。

PR #3 已合并为 `b0dcf94`，同步本地 main 后从 main 创建 `codex/phase2-structural-retrieval`。仓库始终 PRIVATE。

本批只实现 V-11～V-13。V-14（通道开关配置）、V-15～V-18（归一化与融合）、V-19（文件聚合）、V-20～V-25（Recall@K 评估集、CLI 与确定性验收）以及后续 Phase 均未实现，不宣称 Phase 2 整阶段完成。真实本地 Embedding 模型、真实 Embedding API 与 L-18 smoke 仍是独立 NOT RUN 项。

## 接口与行为

**共用图能力**：`RepoGraph` 增加 `adjacent(qname, kinds=STRUCTURAL_KINDS, *, direction="both")`，返回带 confidence 的邻域；`callers` / `callees` / `related_tests` 改为同一内部实现的调用，行为、返回类型与排序与 R-17 冻结接口逐字保持一致（同邻居两向/多 kind 命中时仍取最强、并列取最早行）。三个既有查询与 SQL 的全量比对用例继续通过，新增排序键只在 kind 上打破平局，而单 kind 查询里 kind 恒定，因此不会改变既有顺序。检索侧通过 `retrieval/graph_source.py` 的 `GraphSource` Protocol 消费这一能力，通道单测用内存邻域表（`tests/support/retrieval.py` 的 `FakeGraph`）即可脱离 SQLite 与磁盘仓库运行。

- **V-11 `retrieval/structural.py`**：`StructuralChannel(source, documents, kinds=("CALL","INHERIT","IMPORT"), seed_limit=20, max_depth=2, decay=0.5)`。输入是上游通道的 `RankedSymbol` 排名（service 默认用 BM25 完整排名，也可显式传 `seeds`），按广度优先两向扩展，深度 d 的分数 = 种子分 × 0.5^d。四条刻意决定：① 种子自身不作为深度 0 候选重复回报（否则融合阶段会被两条通道各记一次），但通过边到达的种子算结构证据；② 分数 ≤ 0 的种子不外溢（负余弦乘衰减会反超种子本身，把"不相关"变成"较相关"）；③ 每颗种子各自维护 visited 集，最短路优先，跨种子取最大值，因此结果与文档、边、种子顺序无关；④ 不在候选集里的中间节点仍被穿过（图是仓库的，文档才是可报告的），深度 3 及以上的节点根本不展开。
- **V-12 `retrieval/test_proximity.py`**：`TestProximityChannel(source, documents, kinds=("TEST",))`。证据只有两类，与 02 §8.2 一致：`RetrievalQuery.test_names` 与 `parsed_frames` 中 `in_repo` 的帧；匹配对象限定为 `kind ∈ {test_function, test_method}` 的 symbol（名字像测试的生产函数不算证据）。沿 TEST **出边**（R-12 的方向是 test → 被测 symbol）反推目标，目标本身是测试或不在候选集时丢弃。原始分 = 各命中测试边的 confidence 之和（high 1.0 / medium 0.6 / low 0.3，沿用 R-12 三档，不引入新常数），同一测试对同一目标的多条边只取最强；贡献按测试 id 排序后相加，因为浮点加法不满足结合律，"同输入同数字"是 Phase 2 的验收项。
- **V-13 `retrieval/history.py`**：`HistoryChannel(documents, churn)`，churn 来自 `GitHistory.file_churn()`。原始分 = `math.log1p(文件修改次数)`，单调且次线性（翻倍不到两倍），热门文件不会压过其他通道。窗口内未被触及的文件**不进候选**而不是记 0——否则全仓库都会进入 min-max 归一化的分母，而 0 分表示的其实是"没有证据"。该通道按设计不读 Issue 文本（02 §8.2 输入列写"无"），`search()` 无 query 参数；融合阶段消费的是各通道的候选列表，不要求统一签名。历史隔离完全沿用 R-13：base 必须是完整 SHA、只走祖先、读共享缓存，本批集成与冒烟测试额外断言 base 之后新增文件不进 `file_churn`、因而不进排序。

**service 兼容性**：`retrieve_symbols` 的 `channel` 只增不减（`bm25` / `ripgrep` / `semantic` / `structural` / `test` / `history`），新增 `graph` / `seeds` / `seed_channel` / `churn` 四个可选参数，缺输入时报明确错误（`structural channel requires a graph`、`history channel requires churn from GitHistory.file_churn()`）。既有 bm25 / ripgrep / semantic 的默认值、调用方式、结果契约（`fusion_method=none`、`per_channel` 保留完整候选、`symbols` 截 Top-K、`files` / `channel_weights` 为空）与 CLI 冻结入口都未改动；本批仍通过 Python service 接入，`codepilot retrieve` 的多通道 CLI 参数留给 V-23。`RetrievalQuery`、`RetrievalResult`、`RankedSymbol`、`ChannelScore` 与 `rank_candidates` 排序键（raw 降序，再 file / start_line / qualified_name / symbol_id）保持不变，三个新通道全部复用它，所以解释分在整个候选集上归一化、不随 K 变化。

## 验收证据（2026-10-11）

命令日志、JUnit 与环境记录保存在 [`../evidence/phase2-structural-20261011/`](../evidence/phase2-structural-20261011/)，文件 SHA-256 见 `manifest.json`，统计与退出码见 `acceptance.json`。逐项提交：V-11 `2617e6d`，V-12 `951383f`，V-13 `23e5c41`。

| 验收 | 实际结果 | 证据 |
|---|---|---|
| V-11 衰减系数精确、深度 3 不产生候选、种子不重复回报、仅 CALL/INHERIT/IMPORT、穿越非候选节点、非法参数 | PASS：28 passed（含真实 mini_auth 图 + BM25 种子的两步邻域与确定性断言） | v11.log / v11.xml |
| V-12 测试名与 traceback 测试帧反推、三档 confidence 精确、多测试累加、最强边去重、只读出边、非测试 kind 不作证据 | PASS：18 passed（含 mini_auth 真实 TEST 边两种 Issue 写法） | v12.log / v12.xml |
| V-13 log 归一化单调且次线性、无历史返回空而非报错、未触及文件不占候选、真实 git 的 base 之后提交不进排序 | PASS：15 passed | v13.log / v13.xml |
| 三通道独立与确定性集成（真 git + 真索引 + 真 NetworkX + 真 SQLite） | PASS：5 passed | v13.log / v13.xml |
| 检索与仓库簇相邻回归（retrieval + repo + integration） | PASS：547 passed / 2 skipped | v13.log / v13.xml |
| 冒烟：structural / test / history 与 bm25 在 mini_auth 上各自出分、候选落在声明输入决定的范围内、同输入两次逐字节一致、workspace 干净 | PASS：14 files / 58 symbols；CALL 边 83 条、TEST 边 57 条；结构首位 6.0124 = 种子 12.0248 × 0.5；test 首位 `miniauth.tokens.issue_token` 1.0；history 首位 tokens.py 1.0986 = log1p(2)；base 之后新增文件不在 churn | structural-smoke.py / .log / .json / channel-*.json |
| 完整 pytest + 覆盖率（最终提交树） | PASS：936 passed / 2 skipped / 覆盖率 94.89%（语句 4570/4754，分支 1091/1212），exit 0，耗时 245.29s | final-pytest.log / final-pytest.xml |
| 中间轮全量（收敛 `test_proximity` 冗余分支之前，保留对照） | PASS：936 passed / 2 skipped / 94.88% | full-pytest.log / full-pytest.xml |
| ruff check / ruff format --check / mypy src | PASS（exit 0）/ PASS（139 files already formatted，exit 0）/ PASS（77 source files，exit 0） | ruff.log / format.log / mypy.log |
| `pytest -m docker`（本机） | SKIP：1 skipped / 937 deselected，exit 0——daemon 不可用时按 03 §13 自动跳过，真实容器验收由 CI 执行 | docker.log |
| 模块覆盖率 | structural 100%、test_proximity 100%、history 100%、service 100%、graph_source 88.89%（Protocol 声明体）、`repo/graph.py` 97.75%（唯一未覆盖行是 Phase 1 既有的"边源不在符号表"分支，本批未改 `__init__`） | acceptance.json |
| Linux CI（quality + 离线 pytest + Phase 1 Docker） | PENDING：PR #4 触发后回填真实链接与数字 | — |
| 真实本地 code model 推理 / 真实 Embedding API / 真实 LLM | NOT RUN：本批三个通道不需要 embedding，未产生任何付费调用 | 确定性单测 |
| 04 Phase 2 完整 Recall@K / 融合 / 多通道 CLI 验收 | NOT RUN：依赖 V-14～V-25，超出本批 | 保留后续任务未完成 |

结构、测试邻近、历史三通道现在都能独立构建、独立返回带 `ChannelScore` 的候选；但**没有跑过 Recall@K 评估**，因此不宣称任何通道对定位精度的实际贡献——那需要 V-21～V-24 的评估集与开关配置，消融结论只在实验之后给出（03 §21）。
