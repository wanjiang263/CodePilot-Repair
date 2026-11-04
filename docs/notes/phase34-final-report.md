# Phase34 最终报告：收口修复、v8 归档与全仓回归

结论：CONDITIONAL PASS

- Phase34 引入的 P0/P1/P2 修复全部完成并在最终代码状态上复验。
- v8 候选、manifest、receipt、外部 witness 与 14 版本归档校验通过。
- 全仓九组回归实际执行；**只剩 2 个 eval 失败**，它们在基线 `dba045d`
  上同样复现（`src/codepilot/eval`、`src/codepilot/graph`、`tests/eval`
  相对基线零 diff），不是本轮修改引入。
- 真实 provider、Docker 全量、大仓、24h 长稳仍未验证。

生成时点：C13 `f2a59955bb8272fd8bf19105aeeb951e0adf886a` 之后，C14 之前。
C14 仅包含本报告、Phase34 runtime JSON、静态结果与 delivery receipt（docs-only）。
Phase34 起点：`dba045dabd70f0e6eb7779287136944b589f2734`。

## 一、P0：runtime --check 生命周期（Phase33 缺陷 + Phase34 新证据）

- Phase33 起点复现：`scripts/phase33_runtime_results.py --check` exit 1，
  `sections=["code_commits","head_commit"]`（C4 docs 提交被算作代码）。
  日志：`.codex-pytest-tmp/phase34/pre-fix-check.log`。
- C5 `3a97b98` 修复：`recorded_through` / `phase_commits`、按路径过滤
  `code_commits`、`--check` 只允许记录点之后出现 `docs/` 变化、删除硬编码 base、
  `--write` 幂等。
- Phase33 证据已恢复为 C4 原样（见 §五），Phase34 证据由
  `scripts/phase34_runtime_results.py` 生成，字段使用 `recorded_head` /
  `actual_head_at_write`，不再使用误导性的 `head_commit`。
- 结果：C13 上 `phase34_runtime_results.py --check` exit 0；
  runtime JSON sha256 见 §九。C14 为 docs-only，post-C14 复跑记录于
  `D:/cp34/final/runtime-check.log`。

## 二、可复现性（P1-1）

- 新增 `scripts/verify_archive.sh`：定位仓库根，强制 `PYTHONPATH=<root>/src`，
  选择 `.venv/Scripts/python.exe` 或 `.venv/bin/python`，再 exec verifier。
  共享 venv 的 editable `.pth` 指向主 checkout 的 `src`，这是 worktree 直接执行
  verifier 报 `ImportError: GIT_NORMALIZATION_POLICY` 的根因。
- Windows 深层 worktree 还会触发 `Filename too long`：最终回归使用短 scratch
  `D:/cp34/final`，并设置 `GIT_CONFIG_GLOBAL` 指向临时 `core.longpaths=true` 配置。
  这不是代码修复，而是测试环境修复；报告保留原始首轮失败证据
  （`D:/cp34/final/first-attempt/`）。
- 验收：worktree 干净 shell 执行 `bash scripts/verify_archive.sh ...` 不再
  ImportError；合并到主线后，主 checkout 的 editable 路径指向新代码，普通命令
  也可直接执行（§八）。

## 三、Normalization 契约（P1-2 / P1-3）

- `src/codepilot/archive.py`：新增 `WORKTREE_NORMALIZATION_POLICY`
  （`raw_bytes=worktree`、`raw_hashes_stored=true`），worktree 候选写入该 policy；
  schema 5 继续只接受 git_commit provenance（`SourceOriginError`）。
- `scripts/phase28_archive_verify.py`：
  - 按 `source_origin` 分派校验 git/worktree policy 与对应文案；
  - 无 policy 时只允许“版本 + `candidate_payload_sha256`”命中历史豁免表的
    v3/v4/v5/v6（旧文案），返回 `normalization_note_stale=true`；
  - 新候选即使冒用旧版本名，seal 不同也会失败；
  - `_verify_v3_archive` 也执行该检查并返回 stale 标记；
  - schema 1/2 继续走既有 old/v2 路径，不受影响。
- 测试：`tests/unit/test_phase34_archive_normalization.py` 覆盖非白名单失败、
  白名单 stale、worktree policy 正反例、schema5 worktree 拒绝、schema3 历史
  豁免、CRLF 仅 LF 哈希；归档契约测试 67/67 通过。
- 真实归档预检：13 版本时 v3/v4/v5/v6 均 ok + stale=true，v7 ok + stale=false。

## 四、v8 候选、清单与回执

- 生成（C10 之后）：
  `scripts/phase28_archive.py --version phase34-candidate-v8
  --parent phase34-candidate-v7 --source-commit 7bed256… --diff-base dae25819…
  --schema-version 5 --manifest-kind full`。
- 元数据：pending、parent `phase34-candidate-v7`、schema 5、full、
  `source_commit=7bed25625343e9e0ef43a8175a34bfda3ea0e6d0`（C10）、
  `diff_base=dae25819b4b75292ff56eb4c8dfb5db154522367`（v7 的 source_commit）、
  127 sources、6 changed、0 deleted。
- 4 文件 sha256：
  - candidate.diff `7281578700ce136d05495734f5634a3bd36bf641c72b6a12737aa6d6c4f72f43`
  - candidate.json `8228c87a8e2f873a1386ca64e9045c6ad77d6e248f43743723e20a3e840b0032`
  - snapshot.json `a46cd34e5a9b10a4305b4fa470d2a918ac47aeccfb29096609603f372d8b8858`
  - sources.json `de62d0e745c1f7cb9cd31feca7c9b24eb5ed4f4866c3e9fbed9edd18da5d1c7b`
  - tree `255b9774c631a2e9a52f6ba1eeed4e41b84c041e47ce87b5947669a2a6b2ef19`
- `git apply --check -R` exit 0；v7 四文件 sha 未变；无 active 指针。
- manifest：56 files、无 drift；receipt
  `configs/harness/receipts/phase34-candidate-v8.json` sha256
  `ad088be25cbd1038392706ad960c93e12ac8843b227d235dd7ba0fbfe1ae8241`；
  witness 同值（`D:/cp34/final/external-witness-v8.txt`）。
- 完整校验：exit 0，`versions=14 new=7 old=7 new_failures=0 unexplained=0`、
  `witness=verified historical_integrity=ok`、baseline 56 / mismatch 0、
  v8 checks 32/32、v6 stale=true、v7 stale=false。
  JSON：`D:/cp34/final/verify-v8.json`。

## 五、Phase33 历史证据分离（P2-1 / P2-2）

- `phase33-runtime-results.json` 恢复为 C4 原样（sha256
  `f1c37a32a7a29eb831ea172d36fd5b1c8aba5c934f2594d07fd9456467a23330`）；
  `phase33-delivery-receipt.json` 恢复为 C4 原样（sha256
  `b0a97696d9a9f25f68a7f3aa67eceae85fb7e4697bc8d5d661dc2689e6df8acd`）。
- 新增 `docs/notes/phase33-errata.md` 记录 P0、性能口径与 normalization 更正；
  恢复后的 Phase33 JSON 没有 `recorded_through`，其 `--check` 在 Phase34 HEAD
  会报 `recorded_through_missing`，这是冻结历史的预期行为。
- Phase34 证据独立到 `docs/notes/phase34-runtime-results.json`，
  使用 `recorded_head` / `actual_head_at_write`。

## 六、全仓回归（P1-4）

短 scratch `D:/cp34/final`、`GIT_CONFIG_GLOBAL` 长路径配置、gate 注入 rg；
每组独立 basetemp/junit。实际结果：

| 组 | 结果 | 耗时 |
|---|---|---|
| unit-a | 1535 passed | 172.14s |
| unit-b | 1138 passed, 2 skipped | 283.34s |
| unit-c | 420 passed | 954.41s |
| unit-d | 25 passed | 6.16s |
| eval | **2 failed**, 479 passed, 2 skipped | 780.18s |
| sandbox | 66 passed, 1 deselected | 2.65s |
| integration | 126 passed, 1 deselected | 342.46s |
| api | 117 passed, 1 skipped | 128.72s |
| graph | 266 passed, 13 deselected | 1384.93s |

- 合计：**4172 passed, 2 failed, 5 skipped**（selected 4179）。
  4179 = 评审基线 4168 + 本轮新增 11 个测试
  （Phase34 runtime 7 + normalization 新增 4）。
- **2 个 eval 失败为基线既有**：
  `test_runtime_prompts_and_inference_task_are_sentinel_free`（缺少 diagnoser
  purpose）与 `test_runtime_workspace_git_artifacts_and_checkpoint_are_clean`
  （`harness/snapshot.json` 夹带 `eval/assembly.py` 文本）。
  `git diff dba045d..HEAD -- src/codepilot/eval src/codepilot/graph tests/eval`
  为空；两个用例在基线代码上重跑同样失败。未用 skip/xfail 掩盖；本轮不动
  eval/graph 实现，留作独立修复。
- 环境性首轮失败（已保留证据并复跑绿）：
  - 终端 PowerShell 首轮 unit-a 1 failed、unit-b 49 failed（workspace/git flake），
    Bash + 短路径复跑 1535/1138 全绿；证据 `D:/cp34/final/first-attempt/`。
  - integration 首轮 1 failed：SWE-bench Lite 数据不在 worktree `cache/`；通过
    只读联接主 checkout 数据集后复跑 126 passed。
- 说明：本轮新增测试使 selected 从 4168 增至 4179；不再写“4168 全通过”。

## 七、静态检查

`scripts/phase32_static_checks.py --base dba045d --scratch D:/cp34/final
--docs-out docs/notes/phase34-static-checks.json --tool phase34-static-final`

- 通过：AST、ruff check、ruff format、mypy（changed src/scripts，0 new errors）、
  locale JSON、EOL（LF）、credential scan（仅新增行）、web/src scope。
- `git diff --check` / CRLF-aware 仍命中生成物：
  1. `historical-baseline.json` 沿用既有 CRLF 约定（旧清单也是 CRLF）；
  2. v7/v8 `candidate.diff` 的 unified diff 空 context 行（单个空格）。
  代码文件单独检查 exit 0；未修改脚本、未放宽检查，如实记录。
- 前端：`web/src` 零 diff → “零 diff 跳过”，不写成通过。
- 静态检查 10 个变更 Python 文件；informational mypy 全树 exit 2 作为债务记录，不入门槛。
- static-checks.json sha256：`56174fe60a3689edbc9ecc10a90ac7eaa6f50c4f032ac73923554950358c975a`。

## 八、主线合并与可复现性（P1-5）

- 合并前预检：主 checkout HEAD 恰为 `dba045d`、无 tracked 修改；新增 tracked
  路径与 176 个 untracked 求交集为空（计划新增路径已确认 free）。
- 完成后在主 checkout 执行 `git merge --ff-only claude/phase34-v7-archive-closure-942bb1`，
  主 HEAD = C14；主工作树可见 Phase34 文档、v7/v8 候选与 v8 receipt。
- 合并后在主 checkout（无 PYTHONPATH）复跑：
  `bash scripts/verify_archive.sh --all --ignore-raw-drift --receipt …
  --expect-receipt …`、目标 pytest、`scripts/phase34_runtime_results.py --check`。
  预期全部通过；结果见最终交付消息与 `D:/cp34/final/` 日志。

## 九、提交与最终 HEAD

- 起点 `dba045d`；Phase33 C1–C4 未改写。
- C5 `3a97b98` runtime --check 修复
- C6 `dae2581` normalization 文案 + provider 清理
- C7 `fc1c359` v7 候选
- C8a `00d8058` v7 manifest
- C8b `b485103` v7 receipt
- C9 `62b49a2` Phase33 报告更正（原收口）
- C10 `7bed256` normalization 契约 + verify_archive.sh + Phase34 runtime 生成器
- C11 `75b8671` v8 候选
- C12a `7a23d1d` v8 manifest
- C12b `7ba9b16` v8 receipt
- C13 `f2a5995` 恢复 Phase33 证据 + errata
- C14（本提交）Phase34 runtime JSON、最终报告、静态结果、delivery receipt
- 不 push、不 PR、不激活。
- Phase34 runtime JSON：
  `docs/notes/phase34-runtime-results.json` sha256
  `ae3621c19e22ac8111f71f284b082e2bd7d7c2bb00178c5b565c13f93fcad5d6`；
  `recorded_through = recorded_head = actual_head_at_write = f2a5995`。

## 十、验收清单（逐项）

| # | 项 | 状态 | 证据 |
|---|---|---|---|
| 1 | 最终 HEAD 上 Phase34 runtime `--check` exit 0 | 通过 | C13 上 0；C14 docs-only，post-C14 日志 |
| 2 | 记录点后代码提交 → 非 0 | 通过 | `test_phase34_runtime_results.py` |
| 3 | 记录点后仅 docs → 0 | 通过 | 单测 + C14 结构 |
| 4 | 篡改 `code_commits`/`recorded_head` → 非 0 | 通过 | 单测 |
| 5 | Phase33 报告不再无条件 PASS | 通过 | 顶部 CONDITIONAL PASS + errata |
| 6 | v8 pending、parent=v7、diff_base=dae2581、无 active | 通过 | candidate.json |
| 7 | v8 normalization policy 与实现一致 | 通过 | policy + verify 32/32 |
| 8 | v6/v7 四文件 sha 未变 | 通过 | 逐项哈希 |
| 9 | archive verify 14 versions、new_failures=0、witness verified | 通过 | `verify-v8.json` |
| 10 | 归档/runtime 目标测试通过 | 通过 | 67 + 7 + 10 |
| 11 | 全仓九组实跑，真实计数 | 通过（2 个基线既有 eval 失败） | §六 |
| 12 | 静态检查，CRLF/生成物差异如实记录 | 通过（代码门槛） | §七 |
| 13 | provider fake 通过，real_requests=0、cost=0 | 通过 | provider matrix |
| 14 | 性能口径降级，cold 回退写明 | 通过 | phase33 errata |
| 15 | 本地提交完成，tracked clean | 通过 | C5–C14 |
| 16 | 未 push、未 PR、未激活 | 通过 | 无远端操作、无 active |

## 附：证据索引

- `D:/cp34/final/`：九组 per-group `test-gate-*.json`、gate 日志/junit、
  `static-checks.json`、`provider-matrix.json`、`verify-v8.json`、
  `external-witness-v8.txt`、`first-attempt/`（终端首轮失败）、
  `run-resumable-gate.sh`。
- `.codex-pytest-tmp/phase34/`：pre-fix 日志、v8 生成/校验、early 证据。
- `docs/notes/phase33-errata.md`、`docs/notes/phase33-final-report.md`。
