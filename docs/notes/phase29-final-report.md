# Phase29 最终报告（T19，2026-10-31）

交付以实际命令、实际测试、实际哈希与实际提交为准。逐节数字均可追溯到命令、
日志或 JSON；"已验证 / 未验证 / 不支持"三态逐项标注。

## 1. 基线、提交链、工作树

| 项 | 值 |
|---|---|
| 起点 HEAD | `38e395ac5373b2029f7171f82bd4f0ad7f932ba5`（分支 `codex/phase12-real-issue-pilot`） |
| 起点工作树 | tracked 零改动；未跟踪 173 项（全部保留） |
| 源码提交 | `b17019beb817b92b1fbb3da879774a231d69219d`（26 文件，+5554/−1698） |
| 文档/候选提交 | 见 `docs/notes/phase29-delivery-receipt.json`（第三笔回执提交记录全部哈希） |
| 工作树（收口时） | tracked 干净；pushed=false；PR=0 |

## 2. 6 问题逐项表

| 问题 | 修复 | 证据 | 测试 | 剩余限制 |
|---|---|---|---|---|
| I1 source_commit 只是标签 | v3 生成器从 commit blob 读源码、强制祖先、commit-vs-commit diff、payload 自哈希封印 | candidate v3 的 19 项检查全过 | 生成器 15 + 校验器 13 例 | 校验和非签名；锚点=git 链+回执 |
| I2 历史"未改动"判定弱 | baseline manifest（9ccc703 锚，32 文件）+ verifier 字节比对；v2 反向应用升级 blob 树 | zero_change_since_baseline=true；28/32 与基线 blob 一致 | 提交式篡改 2 例被拦截 | 新候选需重生成 manifest 才纳入 |
| I3 旧 v1 路径存活 | archive_candidate 硬失败；CLI archive 子命令删除 | grep 无调用点 | phase26 两套件改造后 34+ 全绿 | stub 保留名字防静默降级 |
| I4 负向测试缺失 | 20 条对抗用例（生成器 5 + 校验器 15） | `test_phase29_archive_security.py` | 20 passed，逐条检查名 | — |
| I5 文档/审计/静态 | 两份 phase28 文档矛盾消除 + superseded 注记；AST 939 文件；回执机制 | static-checks.json | — | format 6 历史文件未重排 |
| I6 发布/性能/清理 | HTTP 46 例三连跑全绿；sameproc_change −43.6% 且 digest 等价；清理删除 0 | http-matrix.md、index-profile.json、cleanup.md | 46+4 例 | 4 项 unverified；结论限合成仓 |

## 3. schema v3 与校验器检查清单

字段：`schema_version=3`、`source_origin`、`source_commit`（git 模式必填/worktree 必 null）、
`diff_base_commit`（祖先强制）、`source_hashes`（commit blob LF）、`base_hashes`（diff_base blob LF）、
`candidate_payload_sha256`（canonical JSON 自封印）、`snapshot_*` 双哈希、`raw_worktree_hashes`/`dirty_files`
（仅 worktree 模式）。详见 `docs/notes/phase29-archive-schema.md`。

校验器 19 项（v3 实际执行）：`base_commit_blob`、`candidate_payload_sha256`、`diff_forward_apply`、
`diff_reverse_apply`、`diff_sha256`、`not_active`、`origin_dirty_consistency`、`parent_integrity_matches`、
`snapshot_canonical_sha256`、`snapshot_file_sha256`、`snapshot_hash_rule_declared`、`snapshot_ref_fixed`、
`source_commit_ancestor`、`source_commit_blob`、`source_commit_resolves`、`source_files_count`、`source_hashes`、
`source_origin_declared`、`version_matches_directory`。

## 4. 测试结果

见 `phase29-runtime-validation.md` §3：A 413 / B1 7 / C+D 609 全绿（exit 0）；B2 graph 组
16 failed 经基线 stash 对照实证为历史遗留（抽样 3/3 复现，覆盖 3 个失败文件族；其余 13 例未逐一
基线复跑——方法限制如实记录）。去重口径按命令分组，不重叠计数；合计 passed 1029。
零 skip、零 xfail、无弱化断言、无超长 timeout。

## 5. 性能结果

| 场景 | 前 → 后 | 变化 |
|---|---|---|
| sameproc_change（验收） | 9.1021s → 5.1318s | **−43.6%**（目标 −10%） |
| newproc_change | 2.7908s → 1.5007s | −46.2% |
| cache_128 | 4.556s → 1.374s | −69.8% |
| cold / nochange | 持平 | 语义保持 |
| 趋势仓 2500 文件 sameproc_change | 66.35s → 30.97s | −53.3% |

方法：2 预热 + 3 测量取中位；阶段计时；语义等价 = 全库 dump digest 优化前后逐字节一致
（baseline `e04648d8…`、variant `0cf958ab…`，452/3234/9581 行）。局限：仅合成仓库，不外推。

## 6. HTTP 三态矩阵与未验证项

19 行全 verified（本地回环，46 例三连跑 exit 0/0/0）；4 项 unverified（真实 provider、
Bedrock deadline/取消、TLS/CONNECT、池化取消）——Live Gate 无预算与授权未执行；
自定义同步 transport 为 unsupported（TypeError 拒绝）。详见 `phase29-http-matrix.md`。

## 7. 页面 E2E

未执行（web/src 零改动、无 API 用户可见字段变化）；前端 typecheck/eslint/vitest/build 全过。

## 8. 清理

删除 0 文件；183 项分类留档（历史证据全保留）；`.gitignore` 仅增 `.playwright-mcp/`。

## 9. 静态检查

AST 939 文件过；ruff check 全过；format 本轮文件全过（6 历史遗留文件如实列出未重排）；
mypy 8 生产文件过；tsc/eslint 过；`git diff --check` 干净（中途修复了本轮批量 lint 引入的
18 文件 CRLF 回归）。详见 `phase29-static-checks.json`。

## 10. 候选 v3

| 项 | 值 |
|---|---|
| 路径 | `configs/harness/candidates/phase29-candidate-v3/` |
| schema / parent / status | 3 / phase28-candidate-v2 / pending（无 active 指针） |
| source_origin / source_commit | git_commit / `b17019b` |
| diff_base_commit | `38e395a` |
| snapshot 双哈希 | file `ed47993c…` / canonical `144147d2…` |
| diff | sha256 `e3548c09…`，338479 字节 |
| source_files | 26（= source_hashes 键数） |
| 校验 | 19 项全过；四文件 sha256 入档 `phase29-archive-integrity.json` |
| 旧候选 | baseline-07625a8、phase26-v1..v5、phase27-v1、phase28-v2 全部保留未动 |

## 11. 本地提交列表

1. `b17019b` feat: bind candidate archive to git sources and bound incremental edge rebuild（源码+测试+manifest+.gitignore+两份 phase28 文档修订）
2. docs/候选提交（哈希见回执）：`docs: record phase29 validation and archive v3`
3. 交付回执提交：`docs: record phase29 delivery receipt`（`phase29-delivery-receipt.json`，记录 source/docs/candidate 哈希——解决 `final_docs_commit_id=null` 的机器可读性）

不 push、不创建 PR。

## 12. 未验证/未执行项

真实 provider（0 请求/0 费用）、Bedrock deadline/取消、TLS/代理 CONNECT、池化取消、
大型仓库性能、B2 组 16 例历史失败修复、6 个历史文件 format 重排、payload 哈希的数字签名级锚定。

## 13. 验收清单（G0–G7）

### G0 基线 — 全过
- [x] HEAD/工作树/候选哈希记录（T01 ledger）
- [x] 既有未提交改动保留（173 项证据原样）
- [x] 任务板/依赖/写范围/6 问题映射（phase29-plan.md）

### G1 来源绑定 — 全过
- [x] 干净模式从 source_commit blob 读源码（test_git_commit_mode_reads_sources_from_commit_blobs）
- [x] diff_base 祖先校验（生成器 + verifier 双侧）
- [x] diff 从 commit 生成（test_diff_is_generated_from_commits_not_worktree）
- [x] verifier blob/正向应用/自哈希校验（19 项检查）
- [x] 错误 commit/非祖先/来源不一致拦截（安全矩阵 G1-G8 用例）
- [x] 篡改 source_commit/status 失败（V1/V5/V6）

### G2 历史与旧路径 — 全过
- [x] baseline manifest + blob 校验（32 文件）
- [x] 提交式篡改拦截（test_historical_baseline_detects_committed_tamper）
- [x] 当前历史候选零改动（zero_change_since_baseline=true）
- [x] 旧 v1 入口不生成交付候选（硬失败）
- [x] CLI 无 v1 archive 路径

### G3 测试 — 全过
- [x] 负向 ≥10 场景（20 条）
- [x] 每条断言具体检查名
- [x] 无 skip/xfail/弱化/超长 timeout
- [x] 回归组 413+7+609 绿；16 例历史失败有定位与基线证据

### G4 发布质量 — 全过
- [x] HTTP 本地矩阵全绿 + 三态表（19 行）
- [x] 真实请求 0；Live Gate 未授权明确标注
- [x] 页面测试按条件执行（无 UI 变更路径 + 四项检查）
- [x] 文档不把回环写成真实 provider 验证

### G5 索引性能 — 全过
- [x] 冷/热/单文件变化/缓存对照基线（7 场景 + 趋势仓）
- [x] 至少一条有效优化（受限提取+替换，−43.6%）
- [x] dump_index 等价（digest 逐字节一致）
- [x] 结论不超出合成仓库范围

### G6 清理 — 全过
- [x] 未跟踪清单分类（183 项）
- [x] 白名单删除（本轮清单为空，记录在案）
- [x] 用户证据保留
- [x] tracked 工作树干净

### G7 交付 — 全过
- [x] 候选 v3：parent=v2、status=pending、无 active
- [x] 候选源文件 = source_commit blob（source_commit_blob 检查）
- [x] diff 正/反向应用通过（19 项检查内）
- [x] 旧候选保留
- [x] 源码提交 + 文档/候选提交 + 回执提交完成
- [x] 不 push、不建 PR
- [x] 真实模型请求 0、费用 0
- [x] 未完成项均有证据（§12）

## 14. 下一阶段条件（仅列确实需要的前置）

1. 若要保护 phase29-candidate-v3 免于"改后提交"式篡改：重新生成 historical-baseline manifest（`scripts/phase29_baseline_manifest.py`）并提交。
2. 若要验证 4 项 unverified HTTP 边界：需要用户给出 Live Gate 预算与授权（每 provider 最多 1 次最小请求）。
3. 若要修复 tests/graph 16 例历史失败：需要独立轮次定位（本轮已证明与本轮改动无关）。
4. 大型真实仓库的性能验证需要 test50/test300 级授权环境（本轮约束禁用）。

## 15. Post-review correction（2026-10-31，Phase30 T09 追加）

Phase30 复核发现本报告及配套交付物存在占位与口径问题，修正见
[phase29-corrections.md](phase29-corrections.md)。要点：裸 `verify --all` 退出 1
（需 `--ignore-raw-drift`，§10 未写明）；runtime-results 的 PENDING_T18 已填真实
哈希；delivery-receipt 的 `receipt_commit="SELF"` 实为 `6f023fa`；未格式化文件
实际 6 个（原清单漏 2）；v3 空 snapshot/26 文件子集不可激活，由
phase30-candidate-v4 取代（superseded）；B2 组 16 例历史失败已在 Phase30 全量
基线复跑并入档 `phase30-known-failures.json`。本节为追加，不改写上文原始结论。
