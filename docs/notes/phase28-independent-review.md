# Phase28 S8 独立审计报告（对抗式复核，2026-10-31）

> **版本状态（Phase29 T08 注记）**：本文件同时承载三轮审计（首轮对象 `51aae862`、二轮 amend 复核、复审 R1 最终哈希 `770c0dc`）。正文中出现的首轮哈希（`51aae86…`、`2e224de6…`、`b182424a…`）与首轮逐节结论仅是历史记录，**一律以标注 `round3_770c0dc_current` 的最终候选为准**；首轮与二轮的差异条目（D1、N1/N2）已按 §R6 复审关闭，本文最终态以复审章节（R1–R6）为准。

审计角色：S8 独立审计员（只读复核，除本文件外未写/改/删任何仓库文件；零 git commit/push、零网络、零模型请求、零 Docker）。审计对象：源码提交 `51aae862f5c9cc45c9d467b2f92ae725d7af7be8`（首轮 HEAD，后经两轮 amend 演进为最终 `770c0dce6bbaa383f86917ec2d74fd085296eedb`，见 §R6）与候选 `configs/harness/candidates/phase28-candidate-v2/`（pending）。哈希复算脚本（一次性、未提交）：`.codex-pytest-tmp/phase28/s8/verify_hashes.py`（仅标准库 + 显式 sys.path 导入 codepilot.rsi；独立实现 LF 规范化与 canonical JSON，不复用生成器代码路径）。

结论速览：8 项审计中 7 项无差异；发现 1 项内容保真差异（D1：v2 snapshot `prompts={}` 与 v1 非空 prompts 不一致，且文档未披露该结果）与 1 项文档口径差异（D2：Windows 10/11 标注矛盾）；另 2 项清单数字（"1073 去重"、"110"）在交付文档中不存在对应声明（N1/N2，非矛盾）。三个声明哈希、75 文件双哈希、diff 反向应用、历史候选未动、active 指针、测试断言真实性、边界措辞均实测通过。

## 1. 源文件哈希抽样复算 — PASS（并扩展为 75/75 全量）

命令：`.venv/Scripts/python.exe -I .codex-pytest-tmp/phase28/s8/verify_hashes.py`（LF 规范化独立实现：`raw.decode("utf-8").replace("\r\n","\n").replace("\r","\n").encode("utf-8")` 后 SHA256；raw 为文件字节 SHA256）。

抽样 10 个（5 个指定 + 5 个按 seed=20281031 从 candidate.json source_hashes 伪随机抽取，含 web/ 与 phase27 脚本各一）：

| 文件 | LF 哈希 vs source_hashes | raw 哈希 vs raw_worktree_hashes |
|---|---|---|
| src/codepilot/archive.py | 一致（b0b46e83db3e…） | 一致（同 LF） |
| src/codepilot/repo/index.py | 一致（d16c17d91b47…） | 一致（同 LF） |
| src/codepilot/retrieval/runner.py | 一致（f848b9923bf7…） | 一致（同 LF） |
| scripts/phase28_index_profile.py | 一致（212ec22bf5a5…） | 一致（同 LF） |
| tests/integration/phase28/test_phase28_archive_verify_loop.py | 一致（eb167e3469ea…） | 一致（同 LF） |
| web/src/components/panels/HarnessStatus.tsx | 一致（bbe4f835ffb9…） | 一致（0ec4321c5f43…，raw≠LF，CRLF 工作区文件） |
| web/src/components/panels/HarnessStatus.test.tsx | 一致（ccb4a5bef9d1…） | 一致（eb7b0c078260…，raw≠LF） |
| web/src/locales/en-US/index.json | 一致（f77a270cc330…） | 一致（同 LF） |
| src/codepilot/agents/coder.py | 一致（63514f022c17…） | 一致（同 LF） |
| scripts/phase27_agent_case.py | 一致（ddbe72e66496…） | 一致（同 LF） |

扩展复算：`count(source_hashes)=75`、`count(raw_worktree_hashes)=75`、两表键集合相等；全量 75/75 文件两条规则全部与 candidate.json 一致（`FULL-SET result: 75/75 files match both rules`）；`sources.json` 75 键与 source_hashes 键集合相等。CRLF 文件（web 下 tsx/ts 等）LF≠raw 属预期，两表各记其位。

## 2. candidate.diff 复核 — PASS

- `sha256sum`（经脚本复算）：`6046206d16f603c22f63947c15b11ade9a61bd196e6e1a5c70e1985ea2bf70f3`，214987 字节，与声明及 candidate.json 一致。
- `git apply --check -R --whitespace=nowarn configs/harness/candidates/phase28-candidate-v2/candidate.diff` → 退出码 0（可反向应用）。
- `git apply --stat`：`13 files changed, 5150 insertions(+), 18 deletions(-)`；`git apply --numstat` 13 条路径全部 ⊆ 75 文件清单（`diff paths NOT in manifest: []`）。
- 头 20 行：新文件 `scripts/phase28_archive.py`（`new file mode 100644`）开头，与 diff_base=`1e2c4430` 口径相符。
- 清单口径独立复算：`git diff --name-only 1e2c4430..51aae86` 共 71 条，其中 `^(src|scripts|tests|web)/` 13 条（与 runtime-validation.md:22 的 "71 条/13 条在范围内/58 条不入档" 一致）。清单 75 = v1 的 64 条（全部保留、零剔除）∪ 11 条新增；diff 13 条 = 11 新增 + 2 修改（`src/codepilot/repo/index.py`、`tests/unit/repo/test_index.py`，均在 v1 清单内）。

## 3. 候选 JSON 字段与 snapshot 复核 — PASS，含 1 项内容保真差异（D1）

字段逐项（命令同第 1 项脚本）：`schema_version==2`、`status=="pending"`、`parent=="phase27-candidate-v1"`、无 `code_revision` 键、`snapshot_hash_rule=="file_bytes_sha256"`、`source_files==75`、`source_commit=="51aae862f5c9cc45c9d467b2f92ae725d7af7be8"`（完整 SHA）、`diff_base_commit=="1e2c4430aebae7d7cea45f245a5482658ca7eb1f"` — 全部 [OK]。

哈希独立复算：`snapshot_file_sha256`（文件字节）= `2e224de6…779b9`、`snapshot_canonical_sha256`（`json.dumps(sort_keys=True, ensure_ascii=False)` 独立实现）= `b182424a…73e4`，均与声明一致。

`rsi.validate_snapshot(snapshot.json, configs/harness)` 通过；snapshot `schema_version=1`，`experiences=1` 条（`phase26-public-timeout-advice`，L2，`evaluation_exposure=public_only`，status=pending）。experiences 与 `phase27-candidate-v1/snapshot.json` 深度逐字段相等（`experiences v2 == v1`）。`instruction=""` 两代一致。

**D1（差异）**：v2 `snapshot.json` 的 `prompts` 为 `{}`，而 v1 为三个角色的非空系统提示词（coder/planner/reviewer）。

- 文件：`configs/harness/candidates/phase28-candidate-v2/snapshot.json`
- 期望：docs/notes/phase28-runtime-validation.md:16 "prompts 由生成器从 LF 规范化后的 agent 源码重提取"；phase28-archive-schema.md "某个 `agents/<role>.py` 不在源文件清单中时该角色缺省" —— 本轮清单实际包含三个 agent 文件，仅键为 `src/codepilot/agents/<role>.py`。
- 实际：`prompts: {}`。机制：`src/codepilot/archive.py:298-304` `_extract_prompts` 以裸键 `agents/{role}.py` 查询 sources（`.get(..., "")`），与清单键 `src/codepilot/agents/{role}.py` 不匹配，三个角色静默缺省；校验器与 `validate_snapshot` 均不视其为错误。
- 影响：哈希自洽、非完整性问题；但快照 policy 内容相对 v1（及历代快照）丢失三角色提示词，且交付文档"重提取"措辞未披露结果为空。运行时影响有限：`src/codepilot/graph/harness.py:81-90` 对缺失角色回退到 agent 自身 `system_prompt`，行为不变；若未来以该候选为基线再生成，空 prompts 会被继续继承。

## 4. active 指针 — PASS

- `find configs/harness -name 'active*'` 无输出；`ls configs/harness/active.json configs/harness/active.next.json configs/harness/candidates/*/active.json` 全部 "No such file or directory"。
- 本轮新增代码 grep：`archive.py`/`phase28_archive.py`/`phase28_archive_verify.py`/`phase28_index_profile.py` 及相关测试中无任何创建 active 指针的代码；仅 `scripts/phase28_archive_verify.py:334-353` **读取**指针做 `not_active` 检查；`tests/unit/test_phase28_archive_verify.py:367/383` 在 pytest `tmp_path` 写 active.json 属反向用例（验证器应报 exit 1），并断言校验器自身不创建/删除指针（:396-397）。
- 指针引用：全仓 `src/ scripts/ tests/ web/src configs/harness` 范围 grep `phase28-candidate-v2`，仅命中生成器 docstring 的用法示例（`scripts/phase28_archive.py:10`）与候选目录自身四个文件；无任何指针引用该候选。

## 5. 历史候选未动 — PASS

- `git status --porcelain configs/harness/candidates/` 仅 `?? configs/harness/candidates/phase28-candidate-v2/`（--untracked-files=all 下精确为该目录 4 个文件）。
- `git diff 9ccc703 --stat -- configs/harness/` 输出为空（对 untracked 无输出，与上一条互补成立：v2 为新增 untracked，其余目录零 tracked 变更）。
- `git show 9ccc703:configs/harness/candidates/phase27-candidate-v1/candidate.json` 落盘后 `cmp` 工作区文件 → BYTE-IDENTICAL；`git diff 9ccc703 -- configs/harness/candidates/phase27-candidate-v1/` 为空（整个 v1 目录与 9ccc703 一致）。

## 6. 测试抽审 — PASS

- 弱化痕迹 grep（skip/xfail/assert True/恒真断言/pragma）：两个文件零 skip、零 xfail、零 `assert True`；仅 `tests/unit/llm/test_phase28_http_matrix.py:169` 一处 `pragma: no cover`（测试自身配置错误的保护分支）。
- `tests/integration/phase28/test_phase28_archive_verify_loop.py`（4 例，32 断言）：数据完整性断言真实存在——快照单字节篡改后 `exit_code==1` 且 `snapshot_file_sha256` 计入失败、canonical 不失败（:202-209）；diff 字节篡改 `b"return 3"→b"return 9"` 后 CLI 退出 1 且输出 `[fail] … diff_sha256`（:226-234）；干净路径退出 0（:221）；`--dry-run` 退出 0 且档案根零落盘（:265-273）。
- `tests/unit/llm/test_phase28_http_matrix.py`（23 函数 × 16 处双协议参数化 = 39 例，124 断言）：精确 usage 解析（input==10/output==5）、服务端恰好 N 次请求（`server.state.requests == 1/3/5`）、deadline 时间窗断言、row14 对端端口 5/5 互异（:831-836，连接不复用的实测断言）、row10 `response.content == BODY` 字节级比对（:627-631）、row15 Bedrock 结构断言（源码无 `http_lifecycle`/`send_request`）。
- 收集数实测（`pytest --collect-only -q`）：archive 15、archive_verify 13、http_matrix 39、integration/phase28 7，合计 74 = 文档 T11 复跑口径；L1-1 的 67 = 15+13+39。

## 7. 数字交叉核对 — 基本一致，含 1 项文档口径差异（D2）与 2 项无法溯源的清单数字（N1/N2）

已核对并一致：

- 源码提交 SHA `51aae862…`（git log、candidate.json、两份 md、runtime-results.json 一致；基线 `9ccc7032…` 一致；diffstat "13 files, +5150/−18" 与 `git apply --stat` 一致）。
- 75 / 64+11 / diff 13 条（71/13/58 口径）——第 2 项实测一致。
- L1 67+28、L2 7：日志 `.codex-pytest-tmp/phase28/s6a-*.log` 尾行 `67 passed in 65.78s`、`28 passed in 6.49s`、`7 passed in 13.12s`、均 `EXIT=0`，与文档数字一致。
- L3 490/381/39/53：`s6b-l3-*.log` 尾行逐一吻合；integration 首跑 `4 failed, 99 passed in 216.21s`（`s6b-l3-integration.log`，第一现场未覆盖）；修复记录日志 `18 passed` / `702 passed, 1 skipped`（既有 embedding 门控）/ `39 passed`。L3 合计 1066 = 490+381+39+53+99+4（算术自洽）。5 个 s6b 日志 grep 零 skip/xfail。
- 三个哈希 + diff 字节数：candidate.json = runtime-validation.md = runtime-results.json = archive-integrity.json = 本审计独立复算，五方一致。
- T11 复跑 74 项（15+13+39+7）：文档声明与实测收集数一致；amend 记录（af0e772→51aae86、3 文件 ruff format）与 runtime-results.json `bytes_changed_by_amend` 一致。
- guardrails：`real_model_requests=0`、`new_cost_cny=0`、`active_pointer=false`、`pushed=false`、`pr_created=false`、`docker_run=false`、`final_docs_commit_id=null`（附"由聊天交付记录提供"注）——runtime-results.json 与 runtime-validation.md"约束合规"节一致。
- 索引剖析数字：`sameproc_change` wall 2.6714s、322=450−128 解析、闭包复合体 45.9%+12.6%+3.6%+5.5%≈67%、512 对照省解析 0.042s/wall 0.29s(≈11%)、峰值内存 116.7/117.2/117.4MiB —— 均可从 phase28-index-profile.json 直接导出或算出（含 index-decision.md 的"主路径 11.0% = DELETE 5.2% + 主路径 INSERT 5.7%"拆分，即 phases 中 replace_for_sources 总量剔除 TEST 写回归属后所得，与 JSON 一致）。

**D2（差异，文档口径）**：`docs/notes/phase28-validation.md:5` 称"机器为 Windows 10 (10.0.22631)"，而 `docs/notes/phase28-index-profile.json` environment.platform 为 "Windows-11-10.0.22631-SP0"。build 22631 为 Windows 11 23H2，Python platform 判定后者正确；两文档对同一台机器的 OS 标注矛盾。影响：仅环境描述准确性，不影响哈希/测试/结论。

**N1/N2（无法溯源，非矛盾）**：审计清单要求核对的 "T11 1073 去重" 与 "L3 …/110" 两个数字在全部 phase28 文档（md+json）中不存在对应声明（`grep -rn "1073\|110\b"` 于 docs/notes/phase28-* 仅命中无关内容；docs 中 T11 仅声明"复跑 74 项"、L3-5 仅声明"首跑 99 passed / 4 failed、修复后该文件 18 passed"）。文档内部无自相矛盾；仅说明交付文档未承载这两个数字。

**O1（观察）**：修复记录日志（如 `fix-unreadable-l2-unit-retrieval-repo.log`）中的 pytest 路径为 `C:\Users\lenovo\Desktop\CodePilot-Repair\...`，与当前仓库根 `D:\Projects\Desktop\CodePilot-Repair` 不同（工作树曾在别处运行/迁移）。所有哈希均按当前字节独立复算通过，不影响完整性结论，仅作证据溯源注记。

## 8. 声明边界抽审 — PASS

- 无据性能/效果声明：`grep "解决率|提速|提升|快了|加速|成功率|更快"` 于全部 phase28 文档，仅命中否定/限定句。原文行：
  - runtime-validation.md:74 "本地回环验证 ≠ 真实服务商行为；本文不写解决率、不写提速百分比、不写任何未经本轮运行支持的性能数字。"
  - runtime-validation.md:61 "本决策不承诺任何提速百分比。"（"≈11%" 前有"合成子集…绝对耗时不可外推 Django"限定）
  - index-decision.md:71 "本决策不承诺任何提速百分比；性能改进留给满足下述先决条件的后续阶段。"
  - cache-search.md:93 ""第二次更快"≠"缓存命中"，本报告维持这一区分。"
- HTTP 矩阵三态边界：http-matrix.md:59 Bedrock 表"执行路径边界 = verified；deadline/取消语义 = **unverified**…不得声明'已支持'"；:66 "unverified 清单（本轮不声明支持，禁止写"已支持"）"；:77-79 "OpenAI / Anthropic 真实端点行为未验证"。实测 row15 仅为结构断言（源码检查），与文档"结构验证"表述一致。
- 本地验证未写成真实服务商验证：runtime-results.json `not_run_or_unverified.real_providers` "0 requests this round; GLM live link has Phase27 records only…not re-run"；`boundary_statement` "local loopback validation does not equal real-provider behavior; no resolution-rate or speedup-percentage claims are made anywhere in this phase's documents"。

## 审计覆盖与限制

复算为读操作：pytest 仅用 `--collect-only`（收集不执行）；`git apply --check -R` 为检查模式不改工作区；validate_snapshot 为纯校验。未重跑任何测试套件（S6/S6b/S7 的运行结果以日志文件为准做了尾行核对，未独立重放）；"T11 在最终字节上复跑 74 项全绿"由 amend 记录与收集数口径佐证，无独立日志（文档已如实注明 "no separate log retained by S7"）。

---

# 复审（D1 关闭，2026-10-31 第三轮候选）

D1 修复后候选第三次重生成，源码提交经第二轮 amend 为 `770c0dce6bbaa383f86917ec2d74fd085296eedb`（HEAD，2026-10-31 00:25:39 +0800，消息不变，parent 仍 `9ccc703`，13 文件 +5204/−18 —— `git show --stat` 实测）。amend 2 实际只改 2 个文件（`git diff 51aae86..770c0dc --stat`：`src/codepilot/archive.py` +26/−6、`tests/unit/test_phase28_archive.py` +40），与 runtime-results.json `bytes_changed_by_amend_round2` 一致。复审结论：**D1 关闭，全部 6 项通过，无新差异**；首轮遗留观察 O1 不变。

## R1. 三哈希独立复算 + diff 反向应用 — PASS

命令：`.venv/Scripts/python.exe -I .codex-pytest-tmp/phase28/s8/verify_hashes2.py`（LF/canonical 独立实现，同首轮）。

- `snapshot_file_sha256` 复算 = `d0fd3a5ac133637454c87a153aa78ae917204387c5cece0cd8f5c2f8d6de5155`，与 candidate.json 及新声明一致。
- `snapshot_canonical_sha256` 复算 = `def0ec454d6c9258feeef1b940680cb717d14edae3a2811ddd0d4e941d3ca67a`，一致。
- `diff_sha256` 复算 = `f49713553d713fa199230f180f07a27a5f51414a58e2a64e212685db28d45da1`，`wc -c candidate.diff` = 217204 字节，一致。
- `git apply --check -R --whitespace=nowarn candidate.diff` → 退出码 0；`git apply --stat` = `13 files changed, 5204 insertions(+), 18 deletions(-)`；`--numstat` 13 条路径全部 ⊆ 75 清单。
- 扩展：75/75 文件两条哈希规则（LF + raw）全量复算再次全部一致；`sources.json` 75 键与 source_hashes 相等；`rsi.validate_snapshot` 通过。
- 71/13/58 清单口径复测：`git diff --name-only 1e2c4430..770c0dc` = 71 条，范围内 13 条。

## R2. candidate.json 关键字段 — PASS

`schema_version==2`、`status=="pending"`、`parent=="phase27-candidate-v1"`、无 `code_revision`、`snapshot_hash_rule=="file_bytes_sha256"`、`source_files==75`、`source_commit=="770c0dce6bbaa383f86917ec2d74fd085296eedb"`（完整 SHA）、`diff_base_commit=="1e2c4430aebae7d7cea45f245a5482658ca7eb1f"`、`created_from=="phase28_archive"`、`dirty_worktree==false` —— 全部 [OK]。`parent_integrity` 块与首轮一致（v1 `file_bytes_consistent=true`、`canonical_consistent=false`，比较性记录）。

## R3. D1 关闭验证 — PASS

- **prompts 数据层**（本审计自行重算，非引用 S7 结论）：v2 `snapshot.json` 的 `prompts` 恰含 `coder/planner/reviewer` 三个非空字符串键（长度 361/1823/496，与 runtime-results.json `char_lengths` 一致）；与 v1 snapshot 的 prompts 深度相等——prompts 子对象 canonical 字节两侧同为 `00fba76a4f9a2ad1…`（逐字节相等）。`experiences` 仍与 v1 逐字段相等，`instruction=""` 不变。
- **修复代码真实存在**：`src/codepilot/archive.py:298-318` `_extract_prompts` 按路径后缀解析（`:313` `PurePosixPath(key.replace("\\","/")).parts[-2:] == ("agents", f"{role}.py")`；`:315` `key=lambda key: (len(key), key)` 最短键消歧；`:317` 无命中时缺省 `""`），与修复描述逐项对应。
- **回归测试真实存在**：`tests/unit/test_phase28_archive.py:176-205` `test_prompts_resolved_from_prefixed_agent_manifest_keys`（CRLF fixture + `src/codepilot/agents/...` 带前缀清单键，断言 prompts 恰三角色）。
- **收集数**：`pytest tests/unit/test_phase28_archive.py --collect-only -q` → **16 tests collected**；受影响四文件合计 75（16+13+39+7）。
- **独立复跑佐证**：amend 2 记录声称"受影响测试（archive 16 + integration archive_verify_loop 4 = 20 项）全绿"且无保留日志——本审计独立复跑 `.venv/Scripts/python.exe -m pytest tests/unit/test_phase28_archive.py tests/integration/phase28/test_phase28_archive_verify_loop.py -q` → **20 passed in 44.92s**，该声明在最终字节上成立。
- **静态检查独立复核**：`.venv/Scripts/python.exe -m ruff check` 与 `ruff format --check`（两改动文件）均通过。
- 文档同步：`docs/notes/phase28-archive-schema.md:32` prompts 段已更新为后缀解析描述（含带前缀键命中与最短键规则）。

## R4. 历史候选未动 — PASS

`git status --porcelain configs/harness/candidates/` 仍仅 `?? phase28-candidate-v2/`；`git diff 9ccc703 --stat -- configs/harness/` 为空；`git show 9ccc703:configs/harness/candidates/phase27-candidate-v1/candidate.json` 与工作区 `cmp` BYTE-IDENTICAL；v1 整目录 `git diff 9ccc703 -- …phase27-candidate-v1/` 为空。

## R5. 交付文档更新核对 — PASS

- **新哈希五方一致**：candidate.json = runtime-validation.md:12-14 = runtime-results.json:74-77 = archive-integrity.json（校验器重跑：exit 0、8 版本、v2 的 12 项检查全 ok、新哈希入档）= 本审计复算。`phase28-validation.md` 不承载哈希（S6a 执行记录口径），无矛盾。
- **amendment_record 两段**：runtime-results.json:8-11 为数组，Round 1（T11 格式化门禁 → `51aae86`，74 项复跑）+ Round 2（D1 修复 → `770c0dc`，受影响 20 项）。
- **前两轮哈希保留且标注 superseded**：`hashes.history.round1_af0e772` 与 `round2_51aae86` 各带 `superseded_because`，`round3_770c0dc_current: true`；旧哈希 `2e224de6…/b182424a…` 仅以历史身份出现；runtime-validation.md:15 如实记录哈希演变（214363 → 214987 → 217204 字节）并明确"双 snapshot 哈希改变正是 D1 修复生效的证据"。
- **首轮 N1/N2 数字已闭环**：新增 T11 节（runtime-validation.md:63、phase28-validation.md:71-84）记载 A1–A5 去重总数 **1073**（=490+381+39+53+**110**，A5 含 phase28 集成 7 例、排除 2 个 Docker 文件——与首轮 s6b 排除口径的差值恰为 7，算术自洽）与 A6=74（重叠不计入）。`t11-*.log` 六个日志尾行逐一吻合（490/381/39/53/110/74 passed）。
- **无夸大声明回流**：更新后的 runtime-validation.md / runtime-results.json / validation.md 重扫"解决率/提速/已支持"，仅命中否定与 unverified 标注；`boundary_statement` 原样保留。
- **L1 数字口径说明**：L1-1 的 "67 passed（15+13+39）" 为 S6a 当时实跑记录，D1 回归测试后现行套件收集数为 16+13+39=68；runtime-results.json L1 detail 已显式注明 "archive suite now 16 after D1 regression test"，runtime-validation.md:63 亦写明 A6 74 项运行于 amend 2 之前字节、amend 2 后复跑的是 20 项受影响组合——历史记录与现状无混淆，不构成差异。

## R6. 复审差异清单

无新差异。首轮 D1 已关闭（数据、代码、测试、文档四层一致）；D2（Windows 10/11 标注）本轮未见修正、维持首轮定性（文档口径问题，不影响哈希/测试/结论）——首轮该表述仅对当时字节有效（见文首版本状态注记），最终以 R1 复核的 `770c0dc` 候选为准；O1（旧路径日志注记）不变。

## 复审覆盖与限制

本轮新增了测试执行（20 项受影响测试独立复跑、ruff 双检查），其余均为读操作（哈希复算、`--collect-only`、`git apply --check -R`、`git diff/show/cmp`）。未独立重放 A1–A6 全量（以 t11-*.log 尾行与算术自洽佐证）；archive-integrity.json 只保留第三轮校验器运行的最终态（results.json 的 `generations_verified: 3` 为过程性声明，与在档 JSON 无矛盾）。复审脚本：`.codex-pytest-tmp/phase28/s8/verify_hashes2.py`（未提交）。
