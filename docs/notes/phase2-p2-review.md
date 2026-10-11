# Phase 2 审查跟进：两条 P2 边界缺陷修复

2026-10-11。基线 `codex/phase2-retrieval-eval` / `95a3104`（PR #6 HEAD）。GitHub 上的
自动 Codex Review 对本分支提出两条 P2，两条都已在本地核实为真实问题，本轮按
“先补失败测试，再最小修改生产代码”的顺序处理，只改这两个问题，不进入 Phase 3。

## P2-1：裸 Git 仓库被判成普通目录

原判据 `not (local_path / ".git").exists()` 只看 `.git` **子目录**：

- bare repository 根本没有 `.git` 子目录；
- linked worktree 的 `.git` 是一个指向别处的**文件**。

于是 `--repo` 指向 bare 仓库时被当作目录快照，`_snapshot` 把 `objects/`、`refs/`、
`hooks/` 当源码复制进私有副本再 `git init`，索引“成功”却没有任何真实源码候选；
而既有 `WorkspaceManager` 的 `git clone --bare` 本来就能正确处理 bare 来源。

`retrieval/runner.py` 新增 `_is_git_repository()`，把判定交给 Git 本身：
`rev-parse --show-toplevel` 解析出的工作树根就是该目录 → 普通仓库或 worktree；
否则 `--is-bare-repository == true` 且 `--git-dir` 解析到该目录自身 → bare 仓库。
两个条件都要求“仓库就在这一层”，因此位于某个上层仓库里的普通子目录仍是目录快照，
bare 仓库的子目录也不会被误认为仓库；上层环境的 `GIT_DIR` / `GIT_WORK_TREE` 由
`run_git` 统一剥离，判定不受调用环境干扰。

`snapshot` 之外的分支、base commit 解析与隔离、公开接口和错误语义均未改动：
Git 来源仍拒绝 `base_commit=snapshot`，目录快照仍要求 `HEAD` / `snapshot`。

## P2-2：以 `.md` / `.txt` 结尾的 Issue 正文被当成缺失文件

原判据是“单行 + `.md` / `.txt` 后缀”即报错，`--issue "Please fix handling of README.md"`
这类合法正文会在检索之前退出 2。`cli.py` 新增 `_looks_like_path()`：含路径分隔符、
以 `.` / `~` 开头，或整串不含空格的写法才按路径处理；`retrieve` 先做
`Path(issue).is_file()`，真实文件（包括文件名带空格的）照旧读取。
明确指定但不存在的路径（`missing.md`、`docs/missing.md`、`no/such/issue.txt`）
仍给出 `Issue 文件不存在: <path>` 并 exit 2。参数签名与位置参数 `query` 完全未变。

## 测试与门禁

| 项 | 实测 |
|---|---|
| 修复前 repository 形态回归 | **4 failed / 3 passed，exit 1**（bare 四种断言全红；normal / worktree / 纯目录当时已按预期通过） |
| 修复前 issue 文本回归 | **2 failed / 5 passed，exit 1**（`.md`、`.txt` 结尾的单行正文各失败一次） |
| 修复后 `test_retrieval_repository_kinds.py` | **7 passed，exit 0**：bare / 普通 / worktree / 非 Git 目录、bare 的 base commit 隔离（前基 0 命中、后基命中）、Git 来源拒绝 `snapshot`、CLI `execution.source_kind == "git"` |
| 修复后 `test_cli_multichannel_retrieve.py` | **27 passed，exit 0**（含新增 7 项：3 段正文、带空格文件名真实文件、3 条缺失路径 exit 2） |
| Ruff check / format check | PASS，exit 0；164 files already formatted |
| mypy src | PASS，87 source files，exit 0 |
| 完整离线 pytest + 覆盖率 | **1161 passed / 1 skipped / 1 deselected / 0 failed，exit 0**，568.79s；含分支综合覆盖率 **95.10%**（80% 门禁）；`retrieval/runner.py` 98%。唯一 skip 仍是未配置本地语义模型，唯一 deselect 是 Docker 标记 |
| docs/04 两条 CLI（参数原样，解释器改用本地 venv 的 `python -m codepilot.cli`，仍 `UV_OFFLINE=1 UV_NO_SYNC=1`） | 各 exit 0；12 条合成样例完整评估 JSON 与 `phase2-retrieval-eval-20261011` 的 gzip 原始字节**完全相同**（SHA-256 `cdc5d81253cbd0c389275d0dd54c221a462495ab11ff9d49404d832fa301f957`），七行失败数全为 0；`retrieve` stdout 也与旧证据同字节（SHA-256 `13a4d91d81b593a149dd5fc7e4f52c6ac5c5e3cde32af33a076b6a9e6cc624f8`） |

新增共 **14 个 pytest 用例**。`cli.py` 按项目既有覆盖率配置（`omit = ["*/tests/*", "*/cli.py"]`）
不计入覆盖率统计，本轮没有改动该配置。

## 实际遇到的问题

1. 首轮完整 pytest 出现 **5 failed / 1156 passed**：新 CLI 测试显式传了 `--log-level ERROR`，
   在同进程内把根日志级别留在 ERROR，后续 `test_embedding_cache.py` 的 `caplog` 收不到
   WARNING 而失败。修法是本轮新增的 CLI 用例不再传 `--log-level`（与同文件既有用例一致），
   没有放宽任何断言，也没有改生产日志模块；随后完整门禁重跑为 0 failed。
2. red 证据是把两个生产文件临时还原成 `95a3104` 的状态实际跑出来的，修复内容当场备份并
   原样写回，`git diff` 与备份逐字节核对一致；只记录真实退出码，不把失败写成通过。

## NOT RUN

真实语义模型 / Embedding API / GPU / 产品 Docker Sandbox / 真实 LLM / 正式真实问题基准仍
NOT RUN；12 条合成开发样例的指标不是模型效果结论。bare 仓库与 worktree 走的是既有离线
索引与检索路径，没有联网、没有付费调用。远端 CI 结果以推送后 PR #6 的 checks 为准。

完整执行记录见 [证据索引](../evidence/phase2-p2-review-20261011/README.md)。
