# Phase34 最终报告：收口修复、v9 归档与全仓回归

结论：PASS（Phase34 范围内）

范围说明：真实 provider、Docker 全量、大仓（≥2 万文件）、24h 长稳、前端检查仍按
Phase34 指令列为“未验证/不在本轮通过项”。本轮定义的可执行验收（P0/P1/P2 修复、
v9 归档、全仓九组回归、静态门槛、runtime/归档生命周期）已全部通过。

最终代码提交：C17b `975582be9000100717c962e86588e4ade6b806fc`；
C18 仅提交本报告、runtime JSON、静态结果与 delivery receipt（docs-only）。
Phase34 起点：`dba045dabd70f0e6eb7779287136944b589f2734`。

## 一、修复结果

### P0：runtime --check 生命周期（C5 `3a97b98`）

- Phase33 起点复现：`--check` exit 1，`sections=["code_commits","head_commit"]`
  （C4 docs 提交被算作代码）。证据：`D:/cp34/final-v9/pre-fix-check.log`。
- 修复：`recorded_through` / `phase_commits`、按路径过滤 `code_commits`、
  `--check` 只允许记录点之后出现 `docs/` 变化、删除硬编码 base、`--write` 幂等。
- Phase34 证据由 `scripts/phase34_runtime_results.py` 生成，使用
  `recorded_head` / `actual_head_at_write`，不再使用误导性的 `head_commit`。
- 最终 HEAD（C18）上 `phase34_runtime_results.py --check` exit 0；
  C17b 上生成，C18 docs-only 后复跑通过。

### P1-1 可复现性（C10 `7bed256`）

- 新增 `scripts/verify_archive.sh`：强制 `PYTHONPATH=<root>/src`，支持
  `.venv/Scripts/python.exe` / `.venv/bin/python`，再 exec verifier。
- 深层 worktree 的 Windows 路径问题用短 scratch + `core.longpaths` 解决；
  最终回归不再覆盖 `GIT_CONFIG_GLOBAL`（覆盖用户全局配置会破坏 workspace fetch
  测试，见 §三环境修正）。
- 合并到主线后，主 checkout 与 worktree 在**无 PYTHONPATH** 下 runtime `--check`
  与 v8/v9 verifier 均通过。

### P1-2 / P1-3 normalization 契约（C6 `dae2581`、C10 `7bed256`）

- 历史 v3/v4/v5/v6 改为“版本 + `candidate_payload_sha256`”内容级豁免；
  其他缺 policy 的候选一律失败；新候选冒用旧版本名会因 seal 不同而失败。
- schema 3/4 worktree 候选写 `WORKTREE_NORMALIZATION_POLICY`
  （`raw_bytes=worktree`、`raw_hashes_stored=true`）并由 verifier 校验；
  schema 5 明确拒绝 worktree provenance（`SourceOriginError`）。
- `_verify_v3_archive` 也执行 normalization 检查并返回 stale 标记；
  schema 1/2 走既有 old/v2 路径。
- 测试：`tests/unit/test_phase34_archive_normalization.py` 覆盖非白名单失败、
  白名单 stale、worktree policy 正反例、schema5 worktree 拒绝、schema3 历史豁免、
  CRLF 仅 LF 哈希；归档契约测试 67/67 通过。

### P1-4 全仓回归（C15 修复 eval isolation）

此前只跑 unit；本轮补齐 eval/sandbox/integration/api/graph，并修掉两个基线既有的
eval isolation 失败：

1. `test_runtime_prompts_and_inference_task_are_sentinel_free`：fixture 的
   `_RuleSandbox` 原来让“supplemental baseline collect”失败，补丁后的目标执行
   因此直接 PASS，diagnose 分支从未进入。现在按调用栈只在 `run_target_tests`
   节点的第一次调用失败，并让第二次 coder 产生一个**不同**的、仍通过的目标补丁，
   使 diagnose → 第二轮 coder → reviewer 全覆盖。
2. `test_runtime_workspace_git_artifacts_and_checkpoint_are_clean`：
   - `harness/snapshot.json` 夹带 `eval/assembly.py`；`harness.py` 现过滤
     `eval/` 源文本（评测模块不是推理侧输入）。
   - `audit_artifact_tree` 把隔离审计自己的负向探测 tool-result 当泄漏；
     现只跳过“失败探测”或“search_code 成功但零命中”的 artifact，以及携带
     sentinel pattern 的探测请求；成功的 read/非空 search 仍会被扫描。
3. 之前 eval 的 2 个 workspace 失败来自 `GIT_CONFIG_GLOBAL` 覆盖用户全局 git
   配置；最终运行不再设置该变量，改用短 scratch 解决路径长度。

结果：`tests/eval` **483 passed, 0 failed**（此前 479 passed / 2 failed / 2 skipped）。

### P1-5 主线合并

- FF 前预检：主 checkout HEAD 恰为 `a1ceb34`（上一轮 Phase34 交付）、无 tracked
  修改；新增 tracked 与 176 个 untracked 交集为空。
- 提交完成后在主线执行 `git merge --ff-only claude/phase34-v7-archive-closure-942bb1`，
  主 HEAD = C18；主工作树可见 Phase34 文档、v7/v8/v9 候选与 v9 receipt。
- FF 后在主 checkout（无 PYTHONPATH）复跑 runtime `--check`、verifier 与目标 pytest。

### P2-1 / P2-2 历史证据分离

- Phase33 runtime JSON 与 delivery receipt 恢复为 C4 原样（`f1c37a32…` /
  `b0a97696…`）；新增 `docs/notes/phase33-errata.md`。
- Phase34 证据独立到 `docs/notes/phase34-runtime-results.json`，字段使用
  `recorded_head` / `actual_head_at_write`。
- 说明：runtime JSON 的 `archive_verify_v8` 键名是历史命名，其内容已在本轮
  切换为 **v9** verify（`verify-v9.json`）；报告中明确标注，避免误读。

## 二、v9 归档（C15 `cc0dd9a` → C16 `cabcaa3` → C17a `7c90c9e` → C17b `975582b`）

- `phase34-candidate-v9`：pending、parent `phase34-candidate-v8`、schema 5、full、
  `source_commit=cc0dd9a9e0b9d61d9d897b0584ee4c54f6c89af2`（C15）、
  `diff_base=7bed25625343e9e0ef43a8175a34bfda3ea0e6d0`（v8 的 source_commit）、
  129 sources、4 changed、0 deleted。
- 4 文件 sha256：
  - candidate.diff `2231dbda540cc4e09b21e9bcbe66b708fb6b01ff8d32d08f9a7e8d749ccddd43`
  - candidate.json `a61d1592110e4c1668afcecb27b8365503a5e92916295c9ed9685aae5d74aadc`
  - snapshot.json `7483129a946454f3b7e5f254e3aa10b6df39a8b29dd399e8607d8cf1404b6b4b`
  - sources.json `be7d60560f2ff567001b97bb47750dc16ebcddd8c4bd51748a7cc681e312c9fc`
  - tree `751adc16de8a8ec0c91ad29780d91cdb51559574784438a0942734206e209455`
- `git apply --check -R` exit 0；v8/v7/v6 四文件 sha 未变；无 active 指针。
- manifest：60 files、无 drift；receipt
  `configs/harness/receipts/phase34-candidate-v9.json` sha256
  `8bd6795b6fb97d15154849aaa46569d58367edbe11ce6ce39423e6f2a4b03c36`；
  witness 同值（`D:/cp34/final-v9/external-witness-v9.txt`）。
- 完整校验：exit 0，`versions=15 new=8 old=7 new_failures=0 unexplained=0`、
  `witness=verified historical_integrity=ok`、baseline 60 / mismatch 0、
  v9 checks 32/32。JSON：`D:/cp34/final-v9/verify-v9.json`。

## 三、全仓九组回归（C15 代码状态）

短 scratch `D:/cp34/final-v9`、gate 注入 rg；**不设置 `GIT_CONFIG_GLOBAL`**；
每组独立 basetemp/junit。实际结果：

| 组 | 结果 | 耗时 |
|---|---|---|
| unit-a | 1535 passed | 170.35s |
| unit-b | 1138 passed, 2 skipped | 243.76s |
| unit-c | 420 passed | 934.55s |
| unit-d | 25 passed | 6.98s |
| eval | **483 passed** | 791.37s |
| sandbox | 66 passed, 1 deselected | 1.83s |
| integration | 126 passed, 1 deselected | 428.97s |
| api | 117 passed, 1 skipped | 144.60s |
| graph | 266 passed, 13 deselected | 1227.12s |

- 合计：**4176 passed, 0 failed, 3 skipped**（selected 4179）。
  4179 = 评审基线 4168 + 本轮新增 11 个测试
  （Phase34 runtime 7 + normalization 新增 4）。
- 无 skip/xfail 掩盖失败；3 个 skip 为平台/环境既有跳过
  （unit 2：embedding 模型提示、os.mkfifo；api 1：真实 GLM14 产物）。
- 环境修正（均保留证据）：
  - 深层 worktree 的 `Filename too long`：短 scratch；
  - SWE-bench smoke 缺数据：只读联接主 checkout 的 `cache/swebench`；
  - `GIT_CONFIG_GLOBAL` 覆盖用户全局配置会破坏 workspace fetch：
    最终运行去掉该覆盖；
  - 终端 PowerShell 首轮 workspace flake：Bash + 短路径复跑绿
    （`D:/cp34/final/first-attempt/`）。

## 四、静态检查

`scripts/phase32_static_checks.py --base dba045d --scratch
.codex-pytest-tmp/phase34/static-final-v9
--docs-out docs/notes/phase34-static-checks.json --tool phase34-static-final`

- 13 个变更 Python 文件；通过：AST、ruff check、ruff format、mypy
  （changed src/scripts，0 new errors）、locale JSON、EOL（LF）、credential scan
  （仅新增行）、web/src scope。
- `git diff --check` / CRLF-aware 仍只命中生成物：
  1. `historical-baseline.json` 沿用既有 CRLF 约定（旧清单也是 CRLF）；
  2. v7/v8/v9 `candidate.diff` 的 unified diff 空 context 行（单个空格）。
  代码文件单独检查 exit 0；未修改脚本、未放宽检查。
- 前端：`web/src` 零 diff → “零 diff 跳过”，不写成通过。
- static-checks.json sha256：
  `5d1724285d774e9da813421e870f5c6118899b52809bdd7e570ebdc82d5de0bc`。

## 五、提交与最终 HEAD

- 起点 `dba045d`；Phase33 C1–C4 未改写。
- C5 `3a97b98` runtime --check 修复
- C6 `dae2581` normalization 文案 + provider 清理
- C7 `fc1c359` v7 候选；C8a `00d8058` v7 manifest；C8b `b485103` v7 receipt
- C9 `62b49a2` Phase33 报告更正（原收口）
- C10 `7bed256` normalization 契约 + verify_archive.sh + Phase34 runtime 生成器
- C11 `75b8671` v8 候选；C12a `7a23d1d` v8 manifest；C12b `7ba9b16` v8 receipt
- C13 `f2a5995` 恢复 Phase33 证据 + errata
- C14 `a1ceb34` v8 收口报告
- C15 `cc0dd9a` eval isolation 修复 + runtime 生成器 v9 支持
- C16 `cabcaa3` v9 候选；C17a `7c90c9e` v9 manifest；C17b `975582b` v9 receipt
- C18（本提交）Phase34 runtime JSON、最终报告、静态结果、delivery receipt
- 不 push、不 PR、不激活。

## 六、验收清单（逐项）

| # | 项 | 状态 | 证据 |
|---|---|---|---|
| 1 | 最终 HEAD 上 Phase34 runtime `--check` exit 0 | 通过 | C17b/C18 均 0 |
| 2 | 记录点后代码提交 → 非 0 | 通过 | `test_phase34_runtime_results.py` |
| 3 | 记录点后仅 docs → 0 | 通过 | 单测 + C18 结构 |
| 4 | 篡改 `code_commits`/`recorded_head` → 非 0 | 通过 | 单测 |
| 5 | Phase33 报告不再无条件 PASS | 通过 | 顶部 CONDITIONAL PASS + errata |
| 6 | v9 pending、parent=v8、diff_base=7bed256、无 active | 通过 | candidate.json |
| 7 | v9 normalization policy 与实现一致 | 通过 | policy + verify 32/32 |
| 8 | v6/v7/v8 四文件 sha 未变 | 通过 | 逐项哈希 |
| 9 | archive verify 15 versions、new_failures=0、witness verified | 通过 | `verify-v9.json` |
| 10 | 归档/runtime 目标测试通过 | 通过 | 67 + 7 + 10 + eval isolation 15 |
| 11 | 全仓九组实跑，0 failed | 通过 | §三 |
| 12 | 静态检查，CRLF/生成物差异如实记录 | 通过（代码门槛） | §四 |
| 13 | provider fake 通过，real_requests=0、cost=0 | 通过 | 166 passed, 0/0 |
| 14 | 性能口径降级，cold 回退写明 | 通过 | phase33 errata |
| 15 | 本地提交完成，tracked clean | 通过 | C5–C18 |
| 16 | 未 push、未 PR、未激活 | 通过 | 无远端操作、无 active |

## 七、未验证（按 Phase34 指令显式排除）

- 真实 provider streaming/usage/billing：0 请求、0 费用；无预算/凭据/授权。
- Docker 全量：未运行（Phase33 最小 e2e 为历史证据）。
- 大型仓库（≥2 万文件）：未验证。
- 24h 长稳：未验证。
- 前端：`web/src` 零 diff，条件跳过。
- `src/codepilot/eval/swebench_runner.py:423` 的 `images.build(path=大目录)` 风险：
  P2，未动。

## 附：证据索引

- `D:/cp34/final-v9/`：九组 `test-gate-*.json`、gate 日志/junit、
  `static-checks.json`、`provider-matrix.json`、`verify-v9.json`、
  `external-witness-v9.txt`、`pre-fix-check.log`、runtime 日志。
- `docs/notes/phase34-runtime-results.json`、`docs/notes/phase33-errata.md`、
  `docs/notes/phase33-final-report.md`。
- v9 receipt：`configs/harness/receipts/phase34-candidate-v9.json`。
