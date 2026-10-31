# Phase29 复核修正（Phase30 T09，2026-10-31）

Phase30 复核发现 Phase29 交付物存在占位符、口径不一致与遗漏。按"不改写历史结
论"原则，原文件不重写（`phase29-delivery-receipt.json` 的 SELF 字段原样保留），
修正以本文件为准；可直接填充占位的文件就地补全并标注 `corrected_in_phase30`。

## C1 裸 `verify --all` 退出 1，需 `--ignore-raw-drift` 才为 0（正文未写明）

- 首次记录：`phase29-final-report.md` §10 称"19 项全过"，`phase29-archive-integrity.json`
  的 `verification_command` 含 `--ignore-raw-drift`，但报告正文未说明裸跑退出码。
- 复核：`phase28-candidate-v2` 的 `raw_worktree_hashes` 相对当前工作区漂移 10 个
  文件（`drifted_files`，合法演进），裸跑按设计判失败。
- 本轮实测（2026-10-31，HEAD 8e1f1f4）：`python scripts/phase28_archive_verify.py --all`
  → **exit 1**（`raw_worktree_hashes` 失败，log：`.codex-pytest-tmp/phase30/verify-bare.log`）；
  加 `--ignore-raw-drift` → exit 0。v4 候选为 git_commit 模式、无 raw_worktree_hashes，
  不依赖该开关。

## C2 `phase29-runtime-results.json` 的 PENDING_T18 占位

- 首次记录：`commits.docs_commit` / `commits.receipt_commit` / `final_docs_commit_id`
  为 `PENDING_T18` 占位。
- 修复：三处已就地填充真实哈希并加 `corrected_in_phase30: true`：
  - `docs_commit` = `3c57bc13daa09ec34fe2ce1f05551354642c09d0`（"docs: record phase29 validation and archive v3"）；
  - `receipt_commit` = `6f023fa304d47b71d42c9066cc1b30daff7759fd`（"docs: record phase29 delivery receipt"）；
  - `final_docs_commit_id` = `3c57bc13daa09ec34fe2ce1f05551354642c09d0`（与交付回执一致）。

## C3 `phase29-delivery-receipt.json` 的 `receipt_commit="SELF"` 不是哈希

- 首次记录：该文件 `commits.receipt_commit` 为字符串 `SELF (见 git log 中本文件所在提交)`。
- 处理：文件不直接改（避免改写已提交回执）；真实回执提交为
  **`6f023fa304d47b71d42c9066cc1b30daff7759fd`**（"docs: record phase29 delivery receipt"，
  即该文件首次入库的提交）。此后所有轮次的回执一律写入真实提交哈希（如 Phase30 的
  `configs/harness/receipts/phase30-candidate-v4.json` + 本文件所在回执提交）。

## C4 `phase29-static-checks.json` 未格式化文件清单不完整（写 6 列 4）

- 首次记录：`pre_existing_unformatted` 列 4 个文件，但 `note` 与最终报告 §12 写"6 个"。
- 复核实测（2026-10-31，`ruff format --check src/ scripts/ tests/`）实际 6 个文件：
  1. `scripts/batch_budget.py`
  2. `scripts/recovery_admin.py`
  3. `tests/graph/test_phase19_static_reliability.py`
  4. `tests/unit/graph/test_phase19_checkpoint_identity.py`
  5. `tests/unit/llm/test_phase19_budget_binding.py`
  6. `tests/unit/tools/test_phase19_repro_permissions.py`
- 缺的是后两个 phase19 测试文件。清单已补全进 `phase29-static-checks.json`
  （加 `corrected_in_phase30: true`）；本轮仍不重排（不做无关重构），Phase30 静态
  门禁按"本轮触碰文件 0 未格式化 + 遗留 6 文件如实列出"口径记录。

## C5 phase29-candidate-v3 不可激活（空 snapshot、26 文件子集）→ 由 v4 取代

- 首次记录：v3 `snapshot.json` 的 `prompts={}`、`experiences=[]`，26 个源文件仅为
  当轮变更子集；v3 虽通过 19 项完整性检查，但任何选择它的激活路径都只能回退到
  代码内置 prompt（v3 的候选来源声明因此失真）。
- 处理：v3 文件全部保留（不修改、不删除），在 Phase30 文档中标注
  **superseded by phase30-candidate-v4**。v4：`candidate_schema_version=4`、
  `kind=full`（96 文件，覆盖 v2∪v3 的 91 个继承文件 + 本轮 11 个变更文件）、
  snapshot 经父链继承 prompts（coder/planner/reviewer 非空）与 1 条 experience、
  生成期严格校验、激活门禁 fail closed。parent 链：v4 → v3 → v2。

## C6 B2 组 16 个历史失败如实记录

- 首次记录：`phase29-final-report.md` §4 已写"16 failed 经基线对照实证为历史遗留"，
  但仅抽样 3/16 复跑基线；其余 13 例未逐一基线复跑。
- 处理：Phase30 T10 在基线 `38e395a` 与 HEAD 上复跑全部 16 个节点，逐条写入
  `docs/notes/phase30-known-failures.json`（字段：nodeid、baseline_commit、
  head_commit、failure、baseline_log、head_log、status=pre-existing、blocking=false）。
  任何后续报告不得写"tests/graph 全过"，必须写 "X passed, 16 known pre-existing
  failures"（若 Phase30 修复了其中可修项，则以 known-failures 文件的实际条目数为准）。

## 与本轮交付的衔接

- 回执：`configs/harness/receipts/phase30-candidate-v4.json`（候选目录之外），
  witness SHA256 见 Phase30 最终报告；`historical_manifest_sha256` 钉住随回执提交
  的 manifest 字节（40 文件，含 v4 的 4 个文件），同步篡改式攻击由 witness 拦截。
- v3 → superseded；v2 及更早候选未做任何改动。
