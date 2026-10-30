# Phase29 执行计划（T01 产出，2026-10-31）

主控：Phase29 执行工程师（单智能体串行执行，无 subagent）。输入：Phase29 任务书（6 问题 I1–I6）、Phase28 交付与审计。
本文件在 T01 写定后随任务推进更新状态列，不改动已写定的基线事实。

## 0. 基线（实测记录，非假定）

- HEAD：`38e395ac5373b2029f7171f82bd4f0ad7f932ba5`（38e395a "docs: record phase28 validation and archive v2"）
- 分支：`codex/phase12-real-issue-pilot`（main 用于 PR，本轮不 push）
- 工作树：tracked 文件零改动（`git status --porcelain` 无非 `??` 行）；未跟踪 173 项（历史轮次遗留 `a10-*.txt`、`.playwright-mcp/` 等，T13 分类处置，本轮默认全部保留）
- 提交链：`9ccc703`(phase27 docs) → `1e2c443`(fix: bound HTTP lifecycles) → `770c0dc`(feat: archive) → `38e395a`(docs)
- 当前候选 `configs/harness/candidates/phase28-candidate-v2/`（保留，不覆盖）：
  - candidate.json 17779 B / sha256 前16 `f45a5d4dfa804c33`
  - snapshot.json 3719 B / `d0fd3a5ac1336374`；sources.json 1384822 B / `a5922f26e973f208`；candidate.diff 217204 B / `f49713553d713fa1`
  - `schema_version=2`、`source_commit=770c0dce…`、`diff_base_commit=1e2c4430…`、`source_files=75`、`status=pending`、无 active 指针
- Python：`.venv/Scripts/python.exe`（3.12.7）；git 2.51.1.windows.1；平台 win32（Git Bash）

## 1. 6 问题 → 任务映射

| 问题 | 一句话现状（证据） | 对应任务 | 目标收口 |
|---|---|---|---|
| I1 source_commit 只是标签 | `archive.py:_read_sources` 读工作区；仅 `rev-parse --verify`；diff 来自工作区 vs diff_base；verifier 无来源校验；实测全零 source_commit + status=active 仍过 | T02/T03/T04/T07 | v3 生成器从 `source_commit` blob 读源码、强制祖先、diff 由 `git diff diff_base source_commit` 生成；verifier 逐文件 blob 校验 + 正向应用 + candidate 自哈希；检查名化失败 |
| I2 历史未改动判定弱 | `verify_old_archive` 用 `git log -1 -- <path>`，提交式篡改不可判 | T05/T07 | `historical-baseline.json` manifest（baseline=`9ccc703` + 逐文件 blob 哈希）；verifier 按当前字节 vs manifest 比对；临时仓库"改后提交"测试必须失败 |
| I3 旧 v1 路径存活 | `rsi.archive_candidate`（rsi.py:312）写 schema v1；rsi CLI `archive` 子命令；phase26 测试调用 | T06 | `archive_candidate` 硬失败并指向新入口；rsi CLI 删除 `archive` 子命令；phase26 测试改为"旧入口不可用于交付"断言 + 直接写 snapshot 夹具 |
| I4 负向溯源测试缺失 | phase28 测试只有正向断言 | T03/T04/T07 | ≥10 条负向用例（错误 commit / 非祖先 / 脏树 / blob 不一致 / 正向应用不一致 / source_commit·status·parent·diff_base 篡改 / 自哈希不一致 / 历史提交篡改），逐条断言具体检查名；无 skip/xfail |
| I5 文档/审计/静态收口 | validation.md "未修改生产代码" 与同文 index.py 修改矛盾；independent-review 首轮 51aae862 旧结论未标 superseded；`final_docs_commit_id=null`；静态无 AST；mypy 仅 3 文件 | T08/T15/T17 | 作用域化措辞；首轮标注 superseded；交付回执 JSON 记录 docs commit；新增 AST 检查脚本并入静态检查；数字全部可追溯 |
| I6 发布质量/索引性能/清理 | HTTP 39 tests 22/5/3 全回环；索引单文件变化≈冷、选了旁路；工作树 173 项未跟踪遗留 | T09/T10/T11/T12/T13 | HTTP 本地矩阵全绿三态表 + 真实请求 0 + Live Gate 未授权标注；索引基线 + 至少一条实测优化（或三路径负结果如实记录"未达成"）；清理清单分类，只删本轮白名单，证据全保留 |

## 2. 任务板（T01–T19，依赖与写范围）

| ID | 状态 | 依赖 | 主要写范围 | 产出/验收 |
|---|---|---|---|---|
| T01 | done | — | 本文件、`.codex-pytest-tmp/phase29/ledger.md` | 基线、映射、任务板 |
| T02 | done | T01 | `docs/notes/phase29-archive-schema.md` | schema v3 字段语义/迁移/拒绝条件 |
| T03 | done | T02 | `src/codepilot/archive.py`、`scripts/phase28_archive.py`、`tests/unit/test_phase29_archive.py` | commit 读源码、祖先校验、commit diff、worktree 模式、错误拦截 |
| T04 | done | T03 | `scripts/phase28_archive_verify.py`、`tests/unit/test_phase29_archive_verify.py` | v3 校验：payload 自哈希、blob、正向应用、检查名 |
| T05 | done | T04 | `scripts/phase29_baseline_manifest.py`（新）、verifier 历史检查、测试 | baseline manifest（32 文件零漂移）+ 提交式篡改拦截；v2 档案反向应用升级为 source_commit blob 树 |
| T06 | done | T03 | `src/codepilot/rsi.py`、phase26 两测试 | v1 入口硬失败、CLI 删除 archive 子命令、旧测试改造 |
| T07 | done | T03–T06 | `tests/unit/test_phase29_archive_security.py` | 20 负向用例（5 生成器 + 15 校验器），逐条检查名 |
| T08 | done | T03–T07 | phase28 两文档、`scripts/phase29_ast_check.py`（新） | 矛盾消除、superseded 注记、AST 检查 939 文件通过 |
| T09 | done | T01 | `scripts/phase29_index_profile.py`（新）、`docs/notes/phase29-index-profile.json` | 基线全绿：sameproc_change 中位 9.1021s；digest 锚点落档 |
| T10 | done | T09 | `src/codepilot/repo/index.py`、`repo/{imports,calls,edges,test_links}.py`、`tests/unit/repo/test_phase29_index_incremental.py` | fast path（受限提取+受限替换+semantics 指纹门控）；数字见 profile 重跑 |
| T11 | done | T01 | `tests/unit/llm/test_phase29_http_matrix.py`、`docs/notes/phase29-http-matrix.md` | 46 例三连跑全绿（0/0/0），三态表 |
| T12 | done | T11 | 无 UI 变更：typecheck/lint/vitest/build 全过 + 说明 | 按条件执行并记录 |
| T13 | done | T01 | `docs/notes/phase29-cleanup.md`、`.gitignore` | 183 项分类，空删除清单 |
| T14 | in_progress | T03–T13 | `.codex-pytest-tmp/phase29/*.log` | repo+storage 412/413 过（1 例 T10 缺陷已修）；graph 16 失败经基线复现实证为历史遗留 |
| T15 | in_progress | T03–T14 | 日志、`docs/notes/phase29-static-checks.json` | AST 939 过；ruff 全过；format 本轮文件全过（6 历史遗留文件不在范围）；mypy 8 文件过；tsc/eslint 过 |
| T16 | pending | T14–T15 | 源码提交 + `configs/harness/candidates/phase29-candidate-v3/` | pending 候选，parent=phase28-candidate-v2，verifier 全过 |
| T17 | pending | T16 | `docs/notes/phase29-runtime-validation.md`、`phase29-runtime-results.json`、`phase29-archive-integrity.json`、`phase29-index-profile.json`、`phase29-final-report.md` | 全数字可追溯 |
| T18 | pending | T17 | git commits（源码、文档/候选、交付回执） | 不 push、不建 PR；只含任务文件 |
| T19 | pending | T18 | 最终报告验收清单 | G0–G7 逐项结果，无未解释项 |

执行顺序：T01 → T02 → T03 → T04 → T05 → T06 → T07 → T08 → T09 → T10 → T11 → T12 → T13 → T14 → T15 → T16 → T17 → T18 → T19。
合并说明：T03+T04 的生成器/校验器对偶测试在各自文件内完成；T07 集中对抗矩阵；T18 分两笔提交（源码+测试 / 文档+候选+回执第三笔），与 4.5 一致。

## 3. 关键设计决定（先行写定，T02 展开）

1. **schema v3**：v2 全部字段保留语义；新增 `source_origin`（`git_commit`|`worktree`）、`base_hashes`（diff_base blob LF 哈希）、`candidate_payload_sha256`（candidate.json 除自身外 canonical JSON 哈希）；`raw_worktree_hashes` 仅 worktree 模式携带；`source_commit` 仅 git_commit 模式必填（worktree 模式为 null）。
2. **生成器两模式**：默认 `git_commit`（要求干净树、祖先关系、blob 读源码、commit-vs-commit diff）；`worktree` 模式必须显式指定（`--allow-dirty` 只在该模式合法），读工作区、记录 dirty_files。失败路径删除半成品目录。
3. **校验器检查名**（新增）：`candidate_payload_sha256`、`source_origin_declared`、`source_commit_resolves`、`source_commit_ancestor`、`source_commit_blob`、`base_commit_blob`、`diff_forward_apply`、`historical_baseline`；保留 v2 既有检查名。
4. **正向/反向应用实现**：临时目录写入 base blob 字节 + `git apply`（stdin，`-C tmpdir`），比对 LF 内容；不触碰真实工作树、不建 git worktree 元数据。
5. **历史 manifest**：`configs/harness/candidates/historical-baseline.json`（T05 生成脚本写定并提交）；verifier 自动发现并逐文件校验当前字节哈希；覆盖 baseline-07625a8、phase26-v1..v5、phase27-v1、phase28-v2 生成时点全部在档文件。
6. **docs commit 回执**：T18 第三笔提交 `docs/notes/phase29-delivery-receipt.json` 记录 source/docs/candidate 哈希，解决 `final_docs_commit_id=null` 的机器可读性（不自引用循环）。
7. **性能**：T09 基线（450 文件合成仓 + 2000 文件趋势仓，2 预热+3 测量中位）；T10 按序尝试批量事务 → 受影响集更新 → 复用未变解析；`dump_index` 逐字段等价是硬门槛；无收益则保留旁路并如实标注"性能目标未达成"。

## 4. 明确不执行项

- 真实 provider / Bedrock / TLS-代理 CONNECT / 池化取消的线上验证：无用户预算与授权，Live Gate 不执行，逐项标"未验证"。
- test50/test300/M4、Docker、官方 Harness、真实模型请求：约束禁止。
- push / PR / active 指针 / 覆盖历史候选：约束禁止。
- `git clean -fd`、删除 `a10-*.txt`、`.playwright-mcp/`：T13 仅清单分类，白名单外不动。
- 大型真实仓库性能外推：结论仅限本轮合成仓库。

## 5. 风险与回退

- **git apply 在仓库外目录的行为**：T03 先以最小实验确认（写入临时目录的 base blob + apply）；不可行则回退为临时 `git init` 仓库方案（同内容预写后 apply）。
- **Windows 临时目录/文件锁**：临时目录用 `tempfile` + try/finally 清理；worktree 方案已排除（避免动 `.git/worktrees`）。
- **v3 校验器对 v2 候选**：`classify()` 按 schema_version 分派；v2 走既有 v2 校验路径不变（历史 phase28-candidate-v2 不受影响）；v3 路径只对 `schema_version==3` 生效。
- **性能三路径全负**：如实记录数据与原因，保留 Phase28 旁路 + 文档化，不改代码制造假收益。
- **回退路径**：任何 T03–T07 失败且 3 条路径内无法修复 → 记录阻塞证据，I1/I2 相关验收标注"未达成 + 证据"，不伪造收口。
