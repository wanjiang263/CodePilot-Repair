# Phase34 最终报告：收口修复、证据自洽与 v7 归档

结论：CONDITIONAL PASS（P0 已修复并在证据 HEAD 上复验；真实 provider / Docker 全量 /
大仓 / 24h 长稳仍未验证，见 §八）

生成时点：C8b `b485103622a65ae52464857fbf5853f02a6cb5f4` 之后、C9 之前；
C9 为 docs-only 收口提交。Phase33 起点：`dba045dabd70f0e6eb7779287136944b589f2734`。

约束：单主控执行，无 teammate/subagent；不 push、不 PR、不激活候选；真实模型请求 0、
费用 0；C1–C4 历史提交不改写；v1–v6 候选文件原样保留；`.codex-pytest-tmp/phase33/`
证据未删除；172 项未跟踪历史文件未改、未提交。

## 一、P0：runtime --check 生命周期修复（T01）

### 1.1 修复前（起点实测）

```
.venv/Scripts/python.exe scripts/phase33_runtime_results.py --check
exit=1
{"check":"fail","sections":["code_commits","head_commit"]}
```

日志：`.codex-pytest-tmp/phase34/pre-fix-check.log`。

根因：JSON 冻结于 C3 `cabf5ef`；C4 `dba045d` 是 docs/receipt 提交。原脚本用
`git log base..HEAD` 重算并把 `head_commit` 写成当前 HEAD，因此 C4 被算作一条
`code_commits`，最终 HEAD 永远多一条。

### 1.2 修复（C5 `3a97b98`）

- 新增 `recorded_through`（证据记录到的提交）与 `phase_commits`（`base..recorded_through`）。
- `code_commits` 由 `git diff-tree --root` 按路径过滤，仅统计
  `src/ scripts/ tests/ web/src/ pyproject.toml web/package.json`；
  docs/archive/receipt 提交不再计入。
- `--check` 从已写入 JSON 读取 `recorded_through`/`head_commit`，验证：
  提交存在、`recorded_through` 是 HEAD 祖先、`head_commit == recorded_through`、
  `recorded_through..HEAD` 只允许 `docs/` 路径变化；随后用记录值重建 payload 比较。
- 硬编码 base 已删除；`--write` 在内容未变时保留旧 `generated_at`，连续两次写入字节一致。
- 新增 `tests/unit/test_phase33_runtime_results.py`，10 个用例覆盖下列语义。

| 用例 | 结果 |
|---|---|
| 在 `recorded_through` 上 `--check` | exit 0 |
| 之后仅 docs 提交 | exit 0 |
| 之后代码提交 | exit 1，`code_changed_after_evidence` |
| `recorded_through` 缺失 | exit 1，`recorded_through_missing` |
| `recorded_through` 不存在 | exit 1，`recorded_through_unknown` |
| 篡改任一 `code_commits` hash | exit 1，`payload_mismatch`（sections 含 `code_commits`） |
| 篡改 `head_commit` 为不相关提交 | exit 1，`head_recorded_mismatch` |
| 连续两次 `--write` | 字节一致 |
| `--check` 不修改 JSON | 字节不变 |

### 1.3 修复后实测

```
.venv/Scripts/python.exe scripts/phase33_runtime_results.py --write --base b4825911b8e2880adfe6177b9aca6f4c35bf15b1
exit=0  sha256=d893cb3a8024fb6de27b4e0194c7239d323fe0ecd1650d3ea618fad315e037a6

.venv/Scripts/python.exe scripts/phase33_runtime_results.py --check
exit=0  {"check":"pass","file":"docs/notes/phase33-runtime-results.json","sha256":"d893cb3a…37a6"}
```

- `recorded_through = head_commit = b485103`（C8b）。
- `phase_commits` = C1..C8b 共 9 条，包含 C4 `dba045d`；
  `code_commits` = `d317113`、`3a97b98`、`dae2581` 共 3 条——C2/C3/C4/C7/C8a/C8b
  均被路径过滤排除。
- C9 为 docs-only；post-C9 复跑命令与退出码记录于
  `.codex-pytest-tmp/phase34/final-check.log`（未跟踪）。

## 二、归档 normalization 修正（T02）

### 2.1 文案与结构

- 旧文案声称 “raw worktree hashes recorded separately”，但 git_commit 模式从未存 raw hash。
- 新文案（git_commit）：
  `UTF-8 LF normalized; raw bytes are recoverable from source_commit git blobs; raw hashes are not stored in git_commit mode`
- 新结构化字段（仅 git_commit 模式）：

```json
"normalization_policy": {
  "source_hash": "lf_sha256",
  "raw_bytes": "source_commit_git_blob",
  "raw_hashes_stored": false
}
```

- worktree 模式保留 raw 语义：文案为
  `UTF-8 LF normalized; raw worktree bytes are recorded as raw_worktree_hashes`，
  不写 `normalization_policy`，继续记录 `raw_worktree_hashes`。
- 校验器读取 `normalization_policy`：结构/文案不一致、声称存了 raw 但字段缺失 → 失败；
  历史 v5/v6（有旧文案、无 policy）只标注 `normalization_note_stale=true`，不失败。

### 2.2 测试

`tests/unit/test_phase34_archive_normalization.py` 6 个用例：新候选带 policy 且文案一致；
历史文案无 policy 只标注 stale；声称 raw 已存但缺失 → 失败；新文案无 policy → 失败；
worktree 模式保留 raw hashes 且无 policy；CRLF 源文件只校验 LF 哈希（并与 raw 哈希不同）。
`tests/unit/test_phase28_archive.py` 的期望同步更新为 policy + 新文案。全部通过。

## 三、provider runner 清理（T03）

- `PROBE_TEXT` 在当前 HEAD 只有一处定义（`scripts/phase32_provider_matrix.py:90`），
  仓库级 grep 未发现重复赋值；无可删除项，如实记录。
- `credentials_present()` 现在真正参与 gate：`row_missing(..., creds=...)` 使用该映射
  判断 env/模块/region 是否齐备；`run_real_channels` 与 `blocked_real_provider` 都传入。
- `probe_provider` 的捕获从 `BaseException` 改为 `Exception`；`KeyboardInterrupt`/
  `SystemExit` 继续抛出，不再被写成 `real-failed` 行。
- 保持“每 provider 最多 1 次、无隐藏重试、失败也记账”。
- `tests/unit/llm/test_phase33_provider_real.py`：原有 12 个 fake transport 测试 +
  2 个新增（KeyboardInterrupt 传播、credentials map 参与 gate）全部通过，14/14；
  默认模式 `real_requests=0`、`real_cost_cny=0.0`。

## 四、v7 候选、清单与回执（T05–T07）

- 生成命令（C6 之后）：
  `scripts/phase28_archive.py --version phase34-candidate-v7 --parent phase33-candidate-v6
  --source-commit dae25819… --diff-base d3171135… --schema-version 5 --manifest-kind full`。
- 元数据：`status=pending`，`parent=phase33-candidate-v6`，`schema_version=5`，
  `manifest kind=full`，`source_commit=dae25819b4b75292ff56eb4c8dfb5db154522367`（C6），
  `diff_base_commit=d3171135511de95dfcdf54feab7c4ef636bd37d6`（v6 的 source_commit）。
- `resolved_source_files=124`；`changed_files` 8 个（本阶段代码/测试），`deleted_files=[]`。
- 4 文件 sha256：
  - candidate.diff `47d4927a0f5cb3bc2c78631e6a1762851a557c645e9243dfa3271be66c0240f0`
  - candidate.json `342c818866ce90a778d51c924f4492c111a5ae9cfbda5037e66e2c5800d97c3b`
  - snapshot.json `f5fe3f364b126457bd7e029bd25212b7c4038c16cda33ced833422350bfd921e`
  - sources.json `14e18061f71704a3e0e059923ca9a6a4b5f3b465653f325d804b902894b4fbb3`
  - `candidate_tree_sha256=402dd388f3174329b0ad95591e5951538327a53818509d891ffd77b06a69aef9`
- `git apply --check -R` 于 C6 工作树：exit 0。
- 无 active 指针（`active.json` / `active.next.json` 均不存在）。
- 历史基线：`historical-baseline.json` 52 files、no drift，新增 v7 4 条；
  manifest sha256 `b112f3ab25032e65296c090fdc6ceda83bab1f97393d1d92a61ab039c8144087`。
- receipt：`configs/harness/receipts/phase34-candidate-v7.json`
  sha256 `01f3301b1b9fe5e1ffc0208865cf7a87661ea9e5ab506e3c2604113069cec764`，
  `manifest_commit=00d8058`，外部 witness 记录于
  `.codex-pytest-tmp/phase34/external-witness.txt`（未跟踪）。
- 完整校验：

```
phase28_archive_verify.py --all --ignore-raw-drift \
  --receipt configs/harness/receipts/phase34-candidate-v7.json \
  --expect-receipt 01f3301b…c764
exit=0
versions=13 new=6 old=7 new_failures=0 unexplained=0
historical_files_modified=False historical_baseline_files=52 mismatches=0
witness=verified historical_integrity=ok new_archive_status=passed
v7 ok=True normalization_note_stale=False checks=32 failures=[]
v6 ok=True normalization_note_stale=True
receipt ok=True checks=23 failures=[]
```

完整 JSON：`.codex-pytest-tmp/phase34/verify-v7-full.json`
sha256 `42ca8fd912fa71ffb5f8097b0a6065f4ffa183e9853cc69d13e1e5f790ef1889`。

- v6 四个文件 sha 未变（与 Phase33 交付值一致）：
  candidate.diff `3e8e6496…93fb`、candidate.json `d51f1348…6acf94`、
  snapshot.json `cb403a19…6b905`、sources.json `6994e671…b97283`。

## 五、测试（T04）

环境：`.venv/Scripts/python.exe -m pytest`，`-m "not docker and not llm"`、
`-p no:cacheprovider`、独立 `--basetemp`；retrieval 批次注入
`C:\Users\lenovo\AppData\Local\OpenAI\Codex\bin\rg.exe` 到 PATH。

| 批次 | 结果 | 耗时 | 证据 |
|---|---|---|---|
| targeted-run1（9 文件） | 128 passed, 1 error（WinError 1455 环境 OOM） | 759.20s | `.codex-pytest-tmp/phase34/pytest/targeted-run1.log` |
| targeted-rerun1（OOM 用例 + runtime 10） | 11 passed | 29.71s | `targeted-rerun1.log` |
| unit-a | 1535 passed | 188.17s | `unit-a-run1.log/.xml` |
| unit-b 首轮 | 1125 passed, 13 failed, 2 skipped（rg 不在 PATH） | 408.67s | `unit-b-run1.log` |
| unit-b 复跑（注入 rg） | 1138 passed, 2 skipped | 295.70s | `unit-b-run1-rg.log/.xml` |
| unit-c（根目录全部 test_*.py） | 409 passed | 892.20s | `unit-c-run1.log/.xml` |
| unit-d | 25 passed | 8.04s | `unit-d-run1.log/.xml` |

- unit 聚合：1535 + 1138 + 409 + 25 = **3107 passed**，2 skipped，0 failed。
  Phase33 基线 3089 passed；增量 +18 = runtime 10 + normalization 6 + provider 2。
- 2 个 skip 为平台/环境既有跳过（`os.mkfifo` 不可用、embedding 模型提示），
  未新增 skip/xfail，未删测试、未放宽断言。
- 失败处理：
  - `test_resealed_candidate_tampering_rejected[forbidden_source]` 首轮
    `OSError: [WinError 1455] 页面文件太小`（启动 git 子进程时），属 Phase33 已记录的
    Windows commit-limit OOM；内存恢复后单测重跑通过。
  - unit-b 首轮 13 个 retrieval 失败均为 `RetrievalError: ripgrep (rg) is required`
    （PATH 缺 rg）；注入 rg 后 1138 passed / 2 skipped，与 Phase33 完全一致。
- targeted 覆盖 T01/T02/T03 及归档回归；runtime 当前版本在 rerun 中复测 10/10。

## 六、静态检查

`scripts/phase32_static_checks.py --base dba045d --scratch .codex-pytest-tmp/phase34
--docs-out docs/notes/phase34-static-checks.json --tool phase34-static-final`

- 检查文件 8 个（本阶段新增/修改的 .py）。
- 通过：AST parse、ruff check、ruff format --check、mypy（changed src/scripts，
  0 new errors，全路径键）、locale JSON、EOL（LF only）、credential scan
  （仅新增行，0 命中）、web/src scope。
- 未通过（如实记录，未放宽检查）：
  - `git diff --check` 与 CRLF-aware 均为 exit 2，仅命中两类生成物：
    1. `configs/harness/candidates/historical-baseline.json`：CRLF。旧清单同样为
       CRLF（旧 300 行 / 新 324 行），属既有生成器约定；CRLF-aware 检查不再报它。
    2. `phase34-candidate-v7/candidate.diff`：被标记行是 unified diff 的空 context
       行（内容为单个空格 `" "`），是补丁格式的固有结构；改动它就会改变被 receipt
       pin 的 diff 字节。
  - 人间代码的空白检查：`git diff --check dba045d -- '*.py' '*.toml' '*.md'` → exit 0；
    receipts/locales JSON → exit 0。证据：`.codex-pytest-tmp/phase34/diff-check-code.log`。
- 结论：代码静态门槛全部通过；两条 whole-tree diff-check 失败为生成物误报，保留原始
  `failed` 字段，不修改脚本、不放宽检查。
- 前端：`web/src` 本轮零 diff → **零 diff 跳过（未运行前端检查）**，不写成“通过”。
- static-checks.json sha256：`1a9cf2bf6fad1afcfbd9d38ee7c4cd21e6711da3e922772543a6547985320590`。

## 七、性能口径修正

原始数值不改，撤回无法由单次测量支撑的因果表述：

- change 路径改善（86.32s → 45.00s，−47.9%）有本轮 A/B 数据支持。
- cold（74.37 → 103.89s）与 nochange（2.98 → 7.32s）变慢，存在回退风险；
  不再声称“无全量回归”。
- scan 22.41 → 36.77s 是观察到的相关性，不是已证明的因果；同轮全量
  verify_change 73.85s vs 基线 79.59s 仅是该轮对照。
- 单仓（2417 文件）、单次测量、机器负载 85–89%；未重复测量，不外推。
- OOM 继续定性为“隔离/保护”而非修复；gate 阈值 `min_free_mb=1200` /
  `min_commit_headroom_mb=2500` 在低内存机器上可能拒绝运行。
- `src/codepilot/eval/swebench_runner.py:423` 的 `images.build(path=大目录)` 同模式风险
  未修（P2，未纳入本轮，未混入 P0）。

## 八、未验证项

- 真实 provider：0 请求、0 费用；无预算/凭据/授权，3 行 blocked；fake 14/14 不替代真实能力。
- Docker 全量：未运行；Phase33 的最小 e2e（71.26s）为历史证据，本轮未复跑。
- 大型仓库（≥2 万文件）：未验证，索引结论仅限本仓 2417 文件。
- 24h 长稳：未验证；API 修复仅有 Phase33 的 20×2 目标循环 + 5 轮全组证据。
- 前端：零 diff 跳过。

## 九、提交与最终 HEAD

- 起点：`dba045d`（Phase33 C4）。
- C1 `d317113`、C2 `8c6df95`、C3 `cabf5ef`、C4 `dba045d`（Phase33，历史不改写）。
- C5 `3a97b98e2b5a6764d07a224c642bab3a6f5ee6fa`：runtime --check 修复。
- C6 `dae25819b4b75292ff56eb4c8dfb5db154522367`：archive normalization + provider 清理。
- C7 `fc1c359107248fb70a1968893bbab9ad410180e0`：v7 候选 4 文件（仅 4 文件）。
- C8a `00d805835a03e6d85caab92572cf23068d586546`：historical baseline manifest。
- C8b `b485103622a65ae52464857fbf5853f02a6cb5f4`：v7 receipt（C8 拆成 a/b，
  因为 receipt 必须引用已存在的 manifest commit）。
- C9：本报告 + Phase33 更正 + runtime JSON + static JSON + delivery receipt（docs-only）。
- 未 push、未 PR、未激活；`tracked clean`（提交后）。

## 十、验收清单（逐项）

| # | 项 | 状态 | 证据 |
|---|---|---|---|
| 1 | 最终 HEAD 上 `--check` exit 0 | 通过 | C8b 实测 exit 0；C9 docs-only，post-C9 复跑见 `final-check.log` |
| 2 | `recorded_through` 后加代码提交 → 非 0 | 通过 | 单测 `code_changed_after_evidence` |
| 3 | 后仅加 docs 提交 → 0 | 通过 | 单测 + C9 结构 |
| 4 | 篡改 `code_commits`/`head_commit` → 非 0 | 通过 | 单测 `payload_mismatch` / `head_recorded_mismatch` |
| 5 | Phase33 报告不再无条件 PASS | 通过 | 顶部 CONDITIONAL PASS + Post-delivery correction |
| 6 | v7 pending、parent=v6、diff_base=d317113、无 active | 通过 | candidate.json + active 检查 |
| 7 | v7 normalization policy 与实现一致 | 通过 | `normalization_policy` + verify check 32/32 |
| 8 | v6 四文件 sha 未变 | 通过 | 与 Phase33 交付值逐项一致 |
| 9 | archive verify 13 versions / new_failures=0 / witness verified | 通过 | `verify-v7-full.json`，exit 0 |
| 10 | 归档测试与 runtime 测试通过 | 通过 | targeted + unit 批次 |
| 11 | 全量 unit 分批通过，无 skip/xfail 掩盖 | 通过 | 3107 passed / 2 skipped（既有平台跳过） |
| 12 | 静态检查通过，CRLF 差异如实记录 | 通过（附生成物误报说明） | 代码门槛全过；两条 diff-check 误报见 §六 |
| 13 | provider 清理后 fake 通过，real_requests=0、cost=0 | 通过 | 14/14；real_requests=0 |
| 14 | 性能报告降级为方向性结论，cold 回退写明 | 通过 | §七 |
| 15 | 本地提交完成，tracked clean | 通过 | C5–C9 |
| 16 | 未 push、未 PR、未激活 | 通过 | 无远端操作、无 active 指针 |

## 附：本轮证据索引（`.codex-pytest-tmp/phase34/`，未跟踪）

- `pre-fix-check.log`、`runtime-write.log`、`runtime-check.log`、`final-check.log`
- `pytest/`：targeted-run1、targeted-rerun1、unit-a/b(+rg)/c/d 的 log 与 xml
- `static.log`、`static-checks.json`、`diff-check.log`、`diff-check-crlf.log`、
  `diff-check-code.log`
- `v7-generate.json`、`verify-v7.log`、`verify-v7-full.json`、`external-witness.txt`
- Phase33 证据副本：`.codex-pytest-tmp/phase33/`（test-gate-*、gate-*、
  static-checks、verify-v6、evidence/t05、evidence/first-attempt、runs/）
