# Phase29 清理清单（T13，2026-10-31）

原则（任务书 §7.9）：先清单、后分类、再白名单删除；不删除用户证据；不运行
`git clean -fd`；清理后 tracked 工作树保持干净（本轮 tracked 零改动，见 T01 基线）。

## 1. 清单快照

- 基线时点未跟踪条目：**173**（`git status --porcelain` 全部为 `??`，tracked 零改动）。
- 本轮执行中新出现的未跟踪条目：**12**（10 个 Phase29 交付物 + 2 个忽略目录内的临时证据，见 §3）。
- 当前总数：183（含被 `.gitignore` 覆盖、不显示在 status 里的 `.codex-pytest-tmp/`、`pytest-of-*/`）。

## 2. 分类与处置

| 类别 | 条目 | 数量 | 处置 | 理由 |
|---|---|---|---|---|
| 历史轮次执行证据 | `a4-*.txt`、`a9-*.txt`、`a10-*.txt` … `a21-*.txt`（进度/状态/清单/运行 ID 记录） | 164 | **原样保留** | 早期会话的交付证据，任务书明令不删；是否归档由用户决定 |
| 历史汇总证据 | `wsl-hashes.txt`、`final-totals.txt`、`final-attempts.txt`、`glm-5.3-flash-main.phase17-official-numpy1264-20261022.json` | 4 | **原样保留** | 同上 |
| 浏览器工具会话产物 | `.playwright-mcp/`（console/page 快照，2026-10-18） | 1 | **保留** + 加入 `.gitignore` | 可再生成的工具缓存，非交付证据；文件不删除，仅停止在 status 中列出 |
| 历史运行日志目录 | `logs/run_evaluation/` | 1 | **原样保留** | 早期运行现场，未获删除授权 |
| Phase29 交付物（本轮新增） | `docs/notes/phase29-plan.md`、`phase29-archive-schema.md`、`scripts/phase29_ast_check.py`、`phase29_baseline_manifest.py`、`phase29_index_profile.py`、`tests/unit/test_phase29_archive.py`、`test_phase29_archive_verify.py`、`test_phase29_archive_security.py`、`tests/unit/llm/test_phase29_http_matrix.py`、`configs/harness/candidates/historical-baseline.json` | 10 | **随 T16–T18 提交** | 本轮交付，不属清理对象 |
| 被忽略的临时证据 | `.codex-pytest-tmp/phase29/`（ledger、日志、剖析原始 JSON、子进程输出） | —（已忽略） | **保留至本轮结束** | 检查点与日志证据；内容为临时，路径本身自 Phase27 起在 `.gitignore` 中 |

## 3. 白名单删除记录

本轮**删除清单为空**：没有任何文件被删除。理由：

1. 任务书将 `a10-*.txt`、`.playwright-mcp/` 等列为未确认文件，删除需用户确认；
2. 本轮自身产生的临时产物全部位于已忽略目录 `.codex-pytest-tmp/phase29/`，
   按惯例保留作为本轮日志证据（ledger.md 的日志路径引用它们）；
3. `git clean -fd` 禁用。

## 4. .gitignore 变更

仅新增一行（可再生成目录，文件保留在磁盘上）：

```
.playwright-mcp/
```

变更后效果：`git status` 的未跟踪清单收敛为历史证据文件 + 本轮交付物；
不改变任何文件的内容或存在性。

## 5. 清理后核验

- tracked 工作树：仍为零改动（本轮只新增文件，未修改既有 tracked 文件以外的东西；
  T18 提交后 tracked 工作树同样干净）。
- 历史候选：`historical-baseline.json` 32 文件零漂移（T05 校验器实测）。
- 用户证据：全部保留（§2 处置列）。
