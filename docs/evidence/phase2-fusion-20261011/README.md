# V-14～V-20 执行证据

范围、接口及算法决定见 [融合模块验收记录](../../notes/phase2-fusion.md)。所有命令使用本地现有 `.venv`。`run-check.py` 捕获真实子进程 stdout / stderr，保存同名 `.log` 和含 command / exit_code 的 `.json`；pytest 同时写 `.xml`。不依赖网络、GPU、Docker、真实 LLM / Embedding API，不做付费调用。

## 证据索引

| 文件前缀 | 内容 |
|---|---|
| `v14-red`～`v20-red` | 各任务实现前的实际 import / collection 失败，pytest exit 2 |
| `v14` | 默认沙箱导致临时夹具权限错误，13 passed / 24 errors，exit 1 |
| `v14-green` | V-14 与单通道 / 既有三通道回归，37 passed，exit 0 |
| `v15` / `v16` / `v17` / `v18` / `v19` | 26 / 29 / 42 / 55 / 28 passed，exit 0 |
| `v20-first` | 三文档夹具无正 BM25 种子，53 passed / 2 failed，exit 1 |
| `v20-k-red` | 非默认 RRF k 未写入快照的失败，1 failed，exit 1 |
| `v20-options-red` | 显式服务参数被忽略的失败，5 failed / 7 deselected，exit 1 |
| `v20` / `v20-final` | 检索簇回归：334 / 339 passed，均 1 skipped，exit 0；后者含新增服务参数覆盖 |
| `ruff` / `format` / `mypy` | 首次全量静态门禁，全通过，exit 0 |
| `full-pytest` | 首次全量收集因新旧 test_config 同名终止，1 error / 1 deselected，exit 2 |
| `final-ruff` / `final-format` / `final-mypy` | 新文件改名后按指定顺序重跑静态门禁，全部通过 |
| `final-pytest` / `final-coverage.json` | 最终 1067 passed / 1 skipped / 1 deselected / 0 failed / 0 errors，exit 0；综合覆盖率 95.12% |
| `acceptance.json` | 从实际 JUnit / 子命令退出码 / coverage JSON 汇总的机器可读验收 |
| `manifest.json` | 本目录文件 SHA-256（不包含 manifest 自身） |

最终门禁顺序：

```text
.venv/Scripts/ruff.exe check .
.venv/Scripts/ruff.exe format --check .
.venv/Scripts/mypy.exe src
.venv/Scripts/python.exe -m pytest -m "not docker and not llm" --cov --cov-report=term-missing --cov-report=json:docs/evidence/phase2-fusion-20261011/final-coverage.json --junitxml=docs/evidence/phase2-fusion-20261011/final-pytest.xml -q
```

JUnit 中 `test_all_six_channels_integrate_repeat_and_roundtrip[weighted/rrf]` 验证六个通道实际接线、连续两次 JSON UTF-8 字节一致、仓库 Symbol 输入反序后输出一致、Pydantic round-trip 与结果一致性复算。Top-K 用例验证原始候选 / 全融合解释不丢失、文件分不受截断影响。旧 CLI / 单通道回归不增加 CLI 参数。

最终新增 **131 个 pytest 用例**，全量语句 4825/5011、分支 1201/1324，综合覆盖率 **95.12%**。ruff exit 0；format 151 files already formatted / exit 0；mypy 81 source files / exit 0。pytest 终端计时 320.93s，JUnit suite 317.671s，exit 0。静态检查记录结束时间依次为 12:15:03、12:15:15、12:15:31，pytest suite 开始时间 12:15:44（2026-10-11，Asia/Shanghai），机器记录见 `acceptance.json`。

`summarize.py` 从 JUnit 统计 passed / failed / errors / skipped，从日志读 deselected，从各命令 JSON 读真实退出码，从 coverage JSON 读取精确统计；同时记录任务 SHA、模块用例数量、原分支 HEAD、冻结文件 diff 和现有运行环境。`manifest.json` 给出目录中全部证据文件的 SHA-256，排除 manifest 自身。测试命名修复提交 `f084a16`，业务实现最后提交 `652028a`；最终只有文档和证据收口提交在其后。

NOT RUN：Docker、真实本地模型、真实 Embedding API、真实 LLM、Linux CI、Recall@K 和多通道 CLI 整阶段验收。当前只有 Python 服务的确定性验证，V-25 仍未完成。PR #4 没有被操作，没有 Push；Phase 2 整阶段仍未完成。
