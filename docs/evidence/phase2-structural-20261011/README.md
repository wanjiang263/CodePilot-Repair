# V-11～V-13 执行证据

范围、接口决定、算法约束与逐项实测记录见 [第三批验收记录](../../notes/phase2-structural-retrieval.md)。`acceptance.json` 保存 JUnit 统计、退出码、覆盖率总数与三个新通道的模块覆盖率，`manifest.json` 保存本目录文件的 SHA-256。

本地 Windows 使用现有 `.venv`：

```text
.venv/Scripts/python.exe -m pytest tests/unit/retrieval/test_structural.py -q --junitxml=.../v11.xml                       # V-11
.venv/Scripts/python.exe -m pytest tests/unit/retrieval tests/integration/test_cli_retrieve.py -q --junitxml=.../v12.xml    # V-12 + 前序回归
.venv/Scripts/python.exe -m pytest tests/unit/retrieval tests/unit/repo tests/integration -q --junitxml=.../v13.xml         # V-13 + 三通道集成 + 相邻回归
.venv/Scripts/python.exe -m pytest -m docker -q                                                                        # docker.log（本机 daemon 不可用 → 1 skipped）
.venv/Scripts/python.exe docs/evidence/phase2-structural-20261011/structural-smoke.py                                   # 真实 git + 真实索引 + 真实图/历史
.venv/Scripts/python.exe -m pytest -q --cov --cov-report=term-missing --cov-report=json:artifacts/phase2-structural/final-coverage.json --junitxml=.../final-pytest.xml
.venv/Scripts/ruff.exe check .
.venv/Scripts/ruff.exe format --check .
.venv/Scripts/mypy.exe src
```

逐项 pytest 同时生成同名 `.xml`，stdout/stderr 重定向到同名 `.log`。`full-pytest.*` 是 `test_proximity.py` 收敛冗余分支**之前**的中间轮全量（936 passed / 2 skipped / 94.88%），`final-pytest.*` 是最终提交树的完整全量，两者都保留以便对照；`v11`~`v13` 的相邻回归覆盖前一批次与本批全部通道。

`structural-smoke.py` 不使用任何测试替身：mini_auth 被真实 `git init` 成仓库，索引写入真实 SQLite（tree-sitter 解析），结构通道查真实 NetworkX 图，历史通道读真实 `GitHistory`，只有 Issue 文本是手写。它在 base 之前多打一次 `tokens.py` 修改以制造 churn 差异，在索引之后再加一个 base 之后的新文件提交，断言该文件既不进 `file_churn` 也不进排序，并且两次运行逐字节一致、workspace `git status --porcelain` 为空。输出为 `structural-smoke.json` / `.log` 与四份 `channel-*.json`（bm25 / structural / test / history 各一份完整 `RetrievalResult`）。

Linux CI（quality、离线 pytest、Phase 1 Docker）由 PR #4 触发，运行结果与提交 SHA 记录在 `docs/notes/phase2-structural-retrieval.md`。本机 Docker daemon 不可用，`pytest -m docker` 自动 skip，真实容器验收只在 CI 完成。真实本地 code model、真实 Embedding API、真实 LLM 与 04 Phase 2 的 Recall@K / 融合 / 多通道 CLI 验收均为 NOT RUN：本批不产生任何付费调用，也不用 Fake 结果宣称检索质量。
