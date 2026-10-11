# Phase 2 审查跟进 P2 修复证据

基线：`codex/phase2-retrieval-eval` / `95a3104`（PR #6 HEAD，Windows 本地）。
说明见 [修复记录](../../notes/phase2-p2-review.md)。

## 实际执行记录

| 检查 | 真实结果 | 证据 |
|---|---|---|
| P2-1 修复前：bare / 普通 / worktree / 非 Git 目录 | **4 failed / 3 passed，exit 1**；bare 的 `source_kind`、源码候选、base 隔离与 `snapshot` 拒绝各失败一次 | `red-repository-kinds.log/json/xml` |
| P2-2 修复前：`--issue` 文本与路径 | **2 failed / 5 passed，exit 1**；以 `.md`、`.txt` 结尾的单行正文各被误判为缺失文件 | `red-issue-text.log/json/xml` |
| P2-1 修复后 | **7 passed，exit 0**；bare 走既有 Git workspace，worktree 与普通仓库不变，纯目录仍为快照，CLI 报告 `execution.source_kind == "git"` | `green-repository-kinds.log/json/xml` |
| P2-2 修复后（整文件） | **27 passed，exit 0**；真实文件仍读取，`missing.md` / `docs/missing.md` / `no/such/issue.txt` 仍 exit 2 | `green-issue-text.log/json/xml` |
| Ruff check / format check | PASS，exit 0；164 files already formatted | `ruff-check.log/json`、`ruff-format.log/json` |
| mypy src | PASS，87 source files，exit 0 | `mypy.log/json` |
| 完整离线 pytest + 覆盖率 | **1161 passed / 1 skipped / 1 deselected / 0 failed，exit 0**，568.79s；含分支综合覆盖率 **95.10%**（80% 门禁），`retrieval/runner.py` 98% | `full-pytest.log/json/xml` |
| 日志状态隔离 | **35 passed，exit 0**；新 CLI 用例（不传 `--log-level`）先执行，再执行 `caplog` 与既有 CLI 回归。首轮完整 pytest 曾因 `--log-level ERROR` 留下全局 ERROR 级别而 5 failed，未放宽断言即修正 | `logging-isolation.log/json/xml` |
| docs/04 两条 CLI 正常结果对比 | `retrieve` / `eval-retrieval` 各 exit 0；评估 JSON 与 `phase2-retrieval-eval-20261011` 的 gzip **原始字节相同**（`cdc5d81253…`），`cli-retrieve.stdout` 与旧证据**同字节**（`13a4d91d81…`），七行失败数全为 0 | `cli-retrieve.*`、`cli-eval.*`、`normal-comparison.json` |

`run-check.py` 保留真实子命令退出码与实际输出；`run-cli.py` 单独保留原始
stdout / stderr。两条 CLI 的参数与 docs/04 原样一致，解释器改用本地
`.venv/Scripts/python.exe -m codepilot.cli`（`UV_OFFLINE=1 UV_NO_SYNC=1
PYTHONIOENCODING=utf-8`，不联网、不安装依赖、不调用模型），输出字节与
`uv run codepilot` 的旧证据一致即为等价性证明。

修复前的 red 记录是把 `src/codepilot/retrieval/runner.py` 与 `src/codepilot/cli.py`
临时还原到基线 `95a3104` 后真实运行得到，修复内容当场备份并原样写回，
`git diff` 与备份逐字节核对一致。

`manifest.json` 由 `write-manifest.py` 生成，覆盖本目录全部文件。

## NOT RUN

真实语义模型 / Embedding API / GPU / 产品 Docker Sandbox / 真实 LLM / 正式真实问题
基准均 NOT RUN；12 条合成开发样例指标不构成模型效果结论。远端 Linux CI 以推送后
PR #6 的 checks 为准。
