# Phase33 Errata（Phase34 追加）

本文档更正 Phase33 交付记录中的已知问题，**不覆盖** Phase33 原始交付物。
Phase33 的 `docs/notes/phase33-runtime-results.json` 与
`docs/notes/phase33-delivery-receipt.json` 已恢复为 C4 `dba045d` 的原始字节。

## 1. P0：runtime --check 在 Phase33 最终 HEAD 失败

- 事实：Phase33 的 runtime JSON 冻结于 C3 `cabf5ef`；C4 `dba045d` 是
  docs/receipt 提交。原脚本每次 `--check` 都用 `git log base..HEAD` 重算，
  并把 `head_commit` 写成当前 HEAD，因此 C4 被算作一条 code commit，
  最终 HEAD 永远多一条 → exit 1。
- 复现（Phase34 起点，日志 `.codex-pytest-tmp/phase34/pre-fix-check.log`）：
  `scripts/phase33_runtime_results.py --check` → exit 1，
  `sections=["code_commits","head_commit"]`。
- 修复：C5 `3a97b98` 引入 `recorded_through` / `phase_commits`，
  `code_commits` 改为按路径过滤，`--check` 读取记录值并只允许其后出现
  `docs/` 变化。
- 因此 Phase33 报告顶部的无条件 PASS 已改为 CONDITIONAL PASS，
  更正节保留在 `docs/notes/phase33-final-report.md#post-delivery-correction-phase34`。

## 2. 为什么 Phase33 JSON / receipt 恢复原样

- 历史交付物不应被后续阶段改写。Phase34 证据改由
  `docs/notes/phase34-runtime-results.json` 承载，生命周期语义相同。
- 恢复后的 Phase33 JSON **没有** `recorded_through`，在 Phase34 HEAD 上运行
  `scripts/phase33_runtime_results.py --check` 会失败
  （`recorded_through_missing`）。这是预期行为：Phase33 证据是冻结历史，
  Phase34 的 `--check` 只针对 Phase34 JSON。
- 恢复后的字节哈希：
  - `phase33-runtime-results.json` =
    `f1c37a32a7a29eb831ea172d36fd5b1c8aba5c934f2594d07fd9456467a23330`
  - `phase33-delivery-receipt.json` =
    `b0a97696d9a9f25f68a7f3aa67eceae85fb7e4697bc8d5d661dc2689e6df8acd`

## 3. 性能口径更正

Phase33 报告中“无全量回归”等表述撤回：

- change 路径改善（86.32s → 45.00s，−47.9%）有该轮 A/B 数据支持。
- cold（74.37 → 103.89s）与 nochange（2.98 → 7.32s）变慢，存在回退风险。
- scan 22.41 → 36.77s 是观察到的相关性，不是已证明的因果。
- 单仓（2417 文件）、单次测量、负载 85–89%，未重复测量，不外推。

## 4. Normalization 契约更正

- Phase33 及更早的 git_commit 候选只存 LF 哈希，却声称“raw worktree hashes
  recorded separately”。新候选必须带 `normalization_policy`。
- 历史 v3/v4/v5/v6 以“版本 + payload seal”白名单豁免，仅标
  `normalization_note_stale=true`，不失败；新候选冒用旧版本名会因 seal
  不同而失败。
- schema 5 继续只接受 git_commit provenance；schema 3/4 的 worktree 候选
  必须带 worktree policy 与 `raw_worktree_hashes`。
- 详细契约与证据见 `docs/notes/phase34-final-report.md` 与
  `docs/notes/phase34-delivery-receipt.json`。

## 5. 交付位置

Phase34 的修复、v8 候选与报告在 Phase34 分支上完成，并 fast-forward 到
`codex/phase12-real-issue-pilot`。Phase33 原始交付仍可在 C4 `dba045d`
及本分支历史中复现。
