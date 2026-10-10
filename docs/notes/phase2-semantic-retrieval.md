# Phase 2 第二批：V-06～V-10

## 范围与 PR 收口

PR #2 的最终 head `487a61d` 与上次已确认的审查版本相同，本地干净、两条审查线程已解决、push/PR 六项检查成功，未发现新增阻断问题，因此跳过重复审查和重复验收。已合并为 `febb2e7d13a962c3c9fc1c2ceddab4b527629e0a`，同步本地 main 后创建 `codex/phase2-semantic-retrieval`；[合并后 main CI](https://github.com/wanjiang263/CodePilot-Repair/actions/runs/38063758481) 的 quality、offline pytest、Phase 1 Docker 均成功。仓库始终 PRIVATE。

本批只实现 V-06～V-10。V-11 起、结构通道、融合、文件聚合、Recall@K、后续 Phase 均未实现，不宣称 Phase 2 整阶段完成。Phase 1 仍为“功能验收完成”；L-18 真实结构化输出与工具调用 smoke 仍是独立 NOT RUN 项，不构成 Phase 1 阻塞。

## 接口与行为

- `EmbeddingProvider` 是同步批量 Protocol：`identity`、`embed(Sequence[str]) -> float64 ndarray`、`close()`。返回矩阵必须为 `(输入数量, dimensions)`，按输入顺序排列、实数且有限。空批次返回 `(0, dimensions)`，不加载模型、不发请求。
- `EmbeddingIdentity(provider, model, dimensions, revision)` 是不可变数据类，四项均由调用方明确配置。revision 是固定模型版本或部署版本；更新权重或服务端同名模型时必须更新 revision。维度为预期输出维度，不向 API 强制发送可选 dimensions 参数。
- V-06：`SentenceTransformerProvider` 延迟导入 `[embeddings]` 与加载模型，默认 CPU、`local_files_only=True`、`trust_remote_code=False`。模型 ID/本地路径、revision、维度均来自调用方，代码未写死模型名。批量 encode 使用空 prompt、float 数值验证，不自动下载模型。真实模型测试为 `slow` 且必须显式提供三个 `CODEPILOT_TEST_EMBEDDING_*` 环境变量。
- V-07：`OpenAICompatibleEmbeddingProvider` 复用 L 层 `ProviderSpec`、`RetryPolicy`、`ConcurrencyLimiter`、允许 provider 白名单与异常类型。向配置 base_url 后追加 `/embeddings`，保留路径前缀；即使注入共享 client，鉴权、额外头与超时也逐次显式设置。请求仅含 model、input 数组、encoding_format=float；默认每批 32 条，batch_size 可配置，批内按 index 恢复、批间维持全局输入顺序，避免一次提交大仓库全部 symbol。响应按 index 恢复顺序，重复/缺失/越界/非整数 index、错误维度、非有限数值明确拒绝。429/5xx/传输失败使用既有重试，4xx 不无谓重试；响应解析错误不重发付费请求。仅关闭自建 client。
- V-08：`CachedEmbeddingProvider(provider, directory)` 用 SHA-256 哈希版本号、完整身份与原始 UTF-8 内容，模型/版本/服务端点/维度之间不复用。可用 `get_settings().cache_dir / "embeddings"` 作为 directory。重复文本在单批次去重，命中后不重算、不重发 HTTP，重启进程仍可命中。只存有限数值 `.npy`，不存源码/密钥；临时文件原子替换、禁止 pickle，损坏项提示并重算。包装器转发 close；provider 身份变化时拒绝继续使用。
- V-09：`SemanticChannel(documents, provider, backend="numpy")` 把完整 `raw_issue` 与 V-03 symbol 文档编码，索引延迟建立、通道生命周期内只建一次。缩放后归一化避免极大/极小向量溢出；零文档向量排除、零 query 返回空。余弦负分保留，裁剪数值误差至 [-1, 1]。沿用 `rank_candidates`：raw 降序，再按 file/start_line/qualified_name/symbol_id；先对完整候选 min-max，再截 Top-K，解释分不随 K 改变。
- V-10：`backend="faiss"` 用 `[faiss]` 的 CPU `IndexFlatIP`，归一化向量转连续 float32。读取全部候选再执行同一排序，避免 Top-K 边界同分遗漏。原生 float32 分数与 NumPy float64 允许数值误差，单测容差 1e-6。缺包、原生库无法加载或可恢复运行错误都会发出 RuntimeWarning，记录 requested_backend/effective backend/fallback_reason，并降级到 NumPy；不隐藏 provider 或输入错误。CI pytest job 安装锁定的 faiss extra，默认运行依赖仍不强制安装。

`RetrievalQuery`、`RetrievalResult`、`RankedSymbol`、`ChannelScore` 未改。`retrieve_symbols` 只追加兼容的 semantic 通道及 `embedding_provider`/`semantic_backend` 可选参数；旧 bm25/ripgrep 默认值与调用保持不变。semantic 仍是单通道 `fusion_method=none`，per_channel 保留完整候选、symbols 取 Top-K、files/weights 为空。CLI 仍保持已冻结的 bm25/ripgrep 入口；本批通过 Python service 接入，完整 CLI 配置留给既有后续任务。

## 验收证据（2026-10-10）

真实命令日志、JUnit、环境、检查退出码与 SHA-256 清单保存在 [`../evidence/phase2-semantic-20261010/`](../evidence/phase2-semantic-20261010/)。逐项提交：V-06 `a723578`，V-07 `23253bf`，V-08 `192925f`，V-09 `59c011d`，V-10 `c3d1245`。

| 验收 | 实际结果 | 证据 |
|---|---|---|
| V-06 桩接口、懒加载、向量验证 | PASS：18 passed / 1 slow NOT RUN | v06.log / v06.xml |
| V-07 MockTransport 批量、乱序、鉴权、超时/重试、非法响应 + V-06 回归 | PASS：37 passed / 1 slow NOT RUN | v07.log / v07.xml |
| V-08 持久命中、模型隔离、去重、损坏恢复、原子写失败 + 前序回归 | PASS：52 passed / 1 slow NOT RUN | v08.log / v08.xml |
| V-09 手算余弦、负/零/极端向量、稳定排序、Top-K、service/CLI 兼容 | PASS：96 passed / 1 slow NOT RUN | v09.log / v09.xml |
| V-10 缺包/加载/构造/搜索/非法返回降级 + 检索回归 | PASS：109 passed / 2 optional NOT RUN（安装 FAISS 前） | v10.log / v10.xml |
| 真实 FAISS 1.15.1 + API 缓存重复输入零次请求 | PASS：34 passed / 0 skipped | faiss-install.log / native-faiss.log / native-faiss.xml |
| 初轮完整 pytest + 覆盖率（分批修复前） | PASS：868 passed / 2 skipped / 93.87% | pytest.log / pytest.xml |
| API 分批边界先失败后通过 | PASS：修复前 2 failed，修复后 36 passed | api-batching-before.log / api-batching-after.log |
| pytest CLI 收集入口修复 | PASS：修复前 2 collection errors；共享桩移至 tests/support 后 113 passed / 1 slow NOT RUN | collection-before.log / collection-after.log / collection-after.xml |
| 最终完整 pytest + 覆盖率 | PASS：870 passed / 2 skipped / 94.59% | final-pytest.log / final-pytest.xml |
| mini_auth 语义 service（合成向量） | PASS：14 files / 58 symbols；最大批 7；第二次 0 HTTP 请求；NumPy 与真实 FAISS 输出相同；fixture Git 干净 | semantic-smoke.py / semantic-smoke.log / semantic-smoke.json / semantic-*.json |
| Linux CI：quality + 离线 pytest + Phase 1 Docker | PASS：865 passed / 6 skipped（5 个 Windows 专属 + 1 slow）/ 1 deselected；覆盖率 94.20%；Docker 1 passed / 0 skipped | implementation-ci.json / implementation-ci-summary.log |
| 本机 Docker | NOT RUN：完整 pytest 自动 skip，daemon 不可用；真实 Docker 已由 CI 执行通过 | final-pytest.log / implementation-ci-summary.log |
| ruff / format / mypy src | PASS / PASS / PASS（73 source files） | ruff.log / format.log / mypy.log |
| 真实本地 code model 推理 | NOT RUN：本机未安装 sentence-transformers / code model，未提供 opt-in 配置 | v06.log |
| 真实 Embedding API / 真实 LLM | NOT RUN：本批采用 Fake/MockTransport，无付费调用 | 确定性单测 |
| 04 Phase 2 完整 Recall@K / 融合验收 | NOT RUN：依赖 V-11～V-25，超出本批 | 保留后续任务未完成 |

不以 Fake/Mock 结果宣称真实模型检索质量，也不以本批单通道验收宣称整个 Phase 2 完成。

业务代码最后修复为 `aa7eaa5`；后续 `3dde3ae` 仅把新增共享测试桩移入 `tests/support`，解决 pytest CLI 搜索路径差异，不改业务接口。前者的 Windows 最终完整测试与后者的 [Linux/Docker CI](https://github.com/wanjiang263/CodePilot-Repair/actions/runs/38065133029) 均实际通过。新增 86 个用例（85 个自动验收 + 1 个真实模型 opt-in）；embeddings 模块覆盖率 98.64%，semantic 模块 100%。

接口用法已核对 [Sentence Transformers 官方源码](https://github.com/huggingface/sentence-transformers/blob/main/sentence_transformers/sentence_transformer/model.py) 与 [FAISS cosine 文档](https://github.com/facebookresearch/faiss/wiki/MetricType-and-distances)。
