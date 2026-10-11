# Phase 2 检索评估与 CLI（V-21～V-25）

基于融合分支 `9688388` 的独立本地开发；不 Push、不操作 PR #4。

V-21 指标以所有任务数为分母，缺失预测 / 失败计未命中，空 Gold 不算 Exact 命中。
Function 目标用 `(file, qualified_name)`，模块级目标用模块 Symbol；Top-K 先去重。
V-22 提供 12 条人工 synthetic-development 样例，Gold 的参考 diff / 摘要 / base
行坐标均在仓库之外，仅由评估模块读取。开发集不作为准确率改进结论。

V-23 保持 `codepilot retrieve "query" --repo ... --channel bm25` 的原始排名与 JSON。
`--issue` 接受 UTF-8 文件或文字，默认启用 BM25 并生成融合 / 文件视图；可用
`--channel bm25,ripgrep,structural,test,history` 或 `--config` JSON 独立选择六通道。
新增 JSON 的 `execution` 保存实际完整 base SHA、快照类型和执行配置，既有
RetrievalResult 字段与服务 Schema 不变。无 Git 元数据的目录在私有副本中生成
固定提交，不初始化原目录；真实 Git 仓库使用既有 fetch 隔离与基准历史查询。

配置示例（RRF 使用 `method: "rrf"`，不能同时指定 `weights`）：

```json
{
  "channels": {"bm25": true, "ripgrep": true, "semantic": true,
               "structural": true, "test": true, "history": true},
  "method": "weighted",
  "embedding": "offline-hash",
  "seed_channel": "bm25",
  "rrf_k": 60
}
```

Semantic 默认关闭；启用时必须显式提供 `offline-hash` 模式。本轮 CLI 不自动
加载模型 / API。它用真实 NumPy 余弦检索确定性的 SHA-256 词频哈希向量，是
**非学习词法基线**，不是训练语义模型的效果，也不是测试 Fake 查表。
服务的既有真实 EmbeddingProvider 接口保持兼容。Structural 单独启用时使用
BM25 Top-20 作为依赖种子，输出只包含 Structural，并明确记录种子来源。

V-24 评估命令默认运行明确标识的 offline-hash 基线，分别启用六个通道，并添加
融合行。`--embedding none` 会将 Semantic 标成 NOT RUN，不填造假的 0 分结果；
融合对应禁用 Semantic。`--ranking rrf` 提供 RRF 对照，`--config` 的权重、种子
与 RRF k 进入实际执行配置。报告保存每个 Issue 的完整 RetrievalResult、执行
基准、整数命中计数、任务总数、版本信息及输入摘要，默认文件为
`artifacts/phase2/retrieval-evaluation.json`。Function 排名采用函数 / 方法 / 测试 /
模块 Symbol 的融合顺序，排除 class；模块级 Gold 用模块 qname。

V-25 使用真实 Git mini_auth Fixture、真实索引 / Graph / GitHistory 与检索融合。
独立 CLI 进程分别在加入未来提交前后运行，固定 base SHA，并改变 PYTHONHASHSEED。
weighted、RRF、原有单通道及评估命令的 stdout / stderr 原始字节均一致，完整
评估 JSON 也一致；去重置顶、文件聚合、JSON round-trip 与未来提交不可达均断言。
命令显式使用 `--log-level ERROR`，INFO 的人类诊断时间戳不属于机器输出验收。

收口边界修复 `a7c7e52`：推理投影前拒绝仓库内 Gold 补丁，校验 diff 新侧坐标，
将实际缺失仓库记为环境失败并留在任务分母。先失败再修复的证据为 `audit-*`。
五项任务各自保留原子提交，未改写既有历史。

2026-10-11 最终本地验收完成。新增 62 项测试（相对融合基线的 JUnit 用例 ID
差集）；完整离线 pytest 为 **1129 passed / 0 failed / 1 skipped / 1 deselected**，
覆盖率 **94.91%**（含分支）。ruff check、format check、mypy src 与 pytest 按顺序
执行，退出码均为 0；mypy 检查 87 个源文件，format 检查 162 个文件。

| 任务 | 实现 / 数据 | 新增测试模块（最终数量） | 原子提交 |
|---|---|---|---|
| V-21 | `retrieval/eval.py` | `test_retrieval_eval.py`（11） | `3394744` |
| V-22 | `retrieval/input.py`、`dataset.py`、12 条 JSONL 与外置参考 diff | `test_retrieval_dataset.py`（20，含收口修复） | `3773f08` |
| V-23 | `cli.py`、`retrieval/runner.py`、`offline.py` | `test_cli_multichannel_retrieve.py`（20） | `44d2a57` |
| V-24 | `cli.py`、`retrieval/evaluation.py` | `test_cli_retrieval_evaluation.py`（10，含收口修复） | `1f10641` |
| V-25 | `test_retrieval_determinism.py`、证据目录的 `verify-determinism.py` | `test_retrieval_determinism.py`（1） | `5d2b763` |

docs/04 原样命令均实际运行，exit 0。使用现有环境，显式设置
`UV_OFFLINE=1 UV_NO_SYNC=1 PYTHONIOENCODING=utf-8`，没有联网安装或模型调用。

```bash
uv run codepilot retrieve --repo tests/fixtures/repos/mini_auth --issue tests/fixtures/issues/expired_token.md
uv run codepilot eval-retrieval --dataset tests/fixtures/retrieval_cases.jsonl
```

第一条默认仅 BM25，输出 20 个 Symbol / 6 个文件，文件首位为
`src/miniauth/tokens.py`。第二条实际运行全部 7 行，各行分母均为 12、失败数均为 0。
下列值依次为 @1 / @5 / @10，原始整数计数与 Exact 指标见完整报告。

| 通道 | File Recall | Function Recall |
|---|---|---|
| BM25 | 0.6667 / 1.0000 / 1.0000 | 0.4167 / 0.9167 / 1.0000 |
| Ripgrep | 0.5000 / 0.7500 / 1.0000 | 0.4167 / 0.7500 / 0.9167 |
| Semantic（offline-hash） | 0.4167 / 0.8333 / 1.0000 | 0.2500 / 0.7500 / 0.8333 |
| Structural（BM25 种子） | 0.4167 / 0.8333 / 0.8333 | 0.1667 / 0.2500 / 0.5000 |
| Test proximity | 0.0833 / 0.1667 / 0.1667 | 0.0833 / 0.0833 / 0.0833 |
| History | 0.1667 / 0.6667 / 1.0000 | 0.0000 / 0.1667 / 0.1667 |
| Fused（默认 weighted） | 0.5833 / 1.0000 / 1.0000 | 0.5833 / 0.6667 / 0.8333 |

这是 12 条合成开发样例上的真实离线检索结果，不是正式性能基准。Semantic 行是
非学习词法哈希基线；Structural 行只返回图扩展候选，但依赖 BM25 Top-20 种子。
目录 Fixture 的 History 只有人工快照提交，不能代表真实仓库的修改规律。
融合 Function@10 低于 BM25，未调整权重追逐这些标签，不声称准确率提升。

按 docs/04 与本轮授权范围，**Phase 2 本地离线阶段出口满足**：实现、纯函数单测、
两条 CLI、通道对照表、真实 Git 基准隔离与完整输出确定性、质量门禁和文档均通过。
真实本地语义模型（相应用例缺模型路径而 skipped）、Embedding API、GPU、产品 Docker
Sandbox（标记用例 deselected）、真实 LLM、远端 CI 均 **NOT RUN**。正式真实问题
基准也未运行；这些不是本轮离线验收的结论。Phase 3 未进入。

实际修正过程保留在证据中：V-22 初始插入补丁的 Gold 行修正为真实基准插入位置；
V-23 RRF 测试配置移除不合法的 weighted 权重；没有修改已有断言掩盖失败。
新增边界失败测试引出的生产修复单独提交为 `a7c7e52`。

完整证据见 [验收索引](../evidence/phase2-retrieval-eval-20261011/README.md)，包括
失败 / 中间失败 / 通过日志、JUnit、覆盖率、原始 CLI 输出、完整报告 gzip 与 SHA-256
清单。原分支 `codex/phase2-fusion` 保持 `9688388`，PR #4 分支保持 `04132b8`。
后续若获授权，Stacked PR 的直接 base 为 `codex/phase2-fusion`；本轮只作本地提交。
