# Phase 2 V-21～V-25 本地证据

日期：2026-10-11。开发基线 `9688388`，分支 `codex/phase2-retrieval-eval`；
经过完整门禁的实现 HEAD 为 `a7c7e52`。任务原子提交：V-21 `3394744`、
V-22 `3773f08`、V-23 `44d2a57`、V-24 `1f10641`、V-25 `5d2b763`。
融合分支保持 `9688388`，PR #4 分支保持 `04132b8`；无 Push、PR 操作或 Phase 3 开发。

各 `*-red`、`*-intermediate`、`*-green` 日志 / JSON / JUnit 保留实际命令、退出码
及失败原因，未改写为 PASS。V-21 / V-22 首次收集因模块尚未实现而失败；V-23 /
V-24 因行为未实现而失败；V-25 因验证脚本尚不存在而失败。
V-22 插入位置与 V-23 RRF 配置的中间失败及纠正有独立记录。`audit-red` /
`audit-green` 记录仓库内 Gold、补丁新侧坐标及实际缺失仓库的边界修复。

## 最终门禁

按顺序执行以下门禁，实际退出码均为 0，原始输出与命令分别在同名 `.log` /
`.json`；pytest 另有 `.xml` 和 `coverage.json`。

| 门禁 | 实际结果 |
|---|---|
| `ruff check .` | PASS |
| `ruff format --check .` | PASS，162 files |
| `mypy src` | PASS，87 source files |
| `pytest -m "not docker and not llm" --cov --cov-report=term-missing` | 1129 passed / 0 failed / 1 skipped / 1 deselected，94.91%（含分支） |

新增 62 项测试：V-21 11、V-22 20、V-23 20、V-24 10、V-25 1。
数量来自与融合基线 JUnit 用例 ID 的差集，V-22 / V-24 包括最终边界修复新增测试。
详细数值与完整提交 SHA 在 [acceptance.json](acceptance.json)。

## docs/04 原样 CLI 验收

```bash
uv run codepilot retrieve --repo tests/fixtures/repos/mini_auth --issue tests/fixtures/issues/expired_token.md
uv run codepilot eval-retrieval --dataset tests/fixtures/retrieval_cases.jsonl
```

两条命令均实际 exit 0，原始字节为 `cli-retrieve.stdout/.stderr`、
`cli-eval.stdout/.stderr`，命令与退出码见对应 `.json`。为复用现有环境设置
`UV_OFFLINE=1 UV_NO_SYNC=1 PYTHONIOENCODING=utf-8`，没有安装依赖或联网调用。
第一条默认仅 BM25，首位文件 `src/miniauth/tokens.py`，20 个 Symbol / 6 个文件。
第二条分母为 12 条 synthetic-development 样例，全部 7 行失败数为 0。

| 通道 | File@1/5/10 | Function@1/5/10 |
|---|---|---|
| BM25 | 0.6667 / 1.0000 / 1.0000 | 0.4167 / 0.9167 / 1.0000 |
| Ripgrep | 0.5000 / 0.7500 / 1.0000 | 0.4167 / 0.7500 / 0.9167 |
| Semantic（offline-hash） | 0.4167 / 0.8333 / 1.0000 | 0.2500 / 0.7500 / 0.8333 |
| Structural（BM25 种子） | 0.4167 / 0.8333 / 0.8333 | 0.1667 / 0.2500 / 0.5000 |
| Test proximity | 0.0833 / 0.1667 / 0.1667 | 0.0833 / 0.0833 / 0.0833 |
| History | 0.1667 / 0.6667 / 1.0000 | 0.0000 / 0.1667 / 0.1667 |
| Fused（weighted） | 0.5833 / 1.0000 / 1.0000 | 0.5833 / 0.6667 / 0.8333 |

`retrieval-evaluation.json.gz` 是默认完整报告的无损 gzip（固定 mtime=0）；解压后的
SHA-256 在 `acceptance.json`，含每个任务 / 通道的原始候选、排序、评分、配置、
基准 SHA、整数命中数、Exact 指标和依赖版本。源报告仍保留于忽略目录
`artifacts/phase2/retrieval-evaluation.json`。Gold 外置并在推理完成后校验。
Semantic 为非学习词法哈希基线；Structural 使用 BM25 Top-20 依赖种子；History
在目录 Fixture 上只有人工快照提交。这些数值不用于声称真实语义模型效果或准确率提升。

## 完整 CLI 确定性与基准隔离

`verify-determinism.py` 使用真实 Git Fixture、索引、Graph 与 GitHistory，在加入
未来提交前后固定 base SHA，独立进程改变 PYTHONHASHSEED，显式 `--log-level ERROR`。
weighted / RRF / 旧单通道 / eval 的 stdout 和 stderr 原始字节均相同，没有去时间戳
或输出清洗；完整评估报告字节也相同。检查置顶去重、文件聚合、JSON round-trip，
并实际断言未来提交在隔离 workspace 不可达。验证 exit 0。

`determinism/` 保存两次运行的命令、原始 stdout / stderr、配置、输入与基准文件摘要、
完整评估报告及 `determinism.json`。该目录的输入副本只是实际运行记录；真实 Git
源 Fixture 保留于忽略目录 `artifacts/phase2/v25-20261011/source`，没有提交嵌套 Git。
`run-check.py` / `run-cli.py` 捕获实际结果；`summarize.py` 验证并生成摘要，
`manifest.json` 保存本证据目录所有其他文件的 SHA-256。

## 出口结论与未运行项

**Phase 2 本地离线出口 PASS**：docs/04 的两个命令、纯函数单测、对照表、结果确定性、
完整离线门禁及文档均完成。真实语义模型、Embedding API、GPU、产品 Docker Sandbox、
真实 LLM、远端 CI、正式真实问题基准均 **NOT RUN**。1 skipped 为未配置真实本地
embedding 模型；1 deselected 为 Docker 标记测试。本轮没有模型付费调用。
后续若获授权创建 Stacked PR，直接 base 为 `codex/phase2-fusion`。
