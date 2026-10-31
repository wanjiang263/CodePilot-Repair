# Phase30 最终报告（候选自证与索引正确性，2026-10-31）

起点 HEAD `6f023fa`；提交链 `38e395a → b17019b → 3c57bc1 → 6f023fa`（Phase29）→
本轮 6 笔（见 §6）。起点 tracked 干净；未跟踪证据全部保留（删除 0 文件）。
真实模型请求 0、费用 0；Live Gate 关闭；无 push、无 PR；无 Docker。

## 1. 结论：PASS

Phase30 完成。三项 P0 全部以"先失败测试、后实现、再验证"关闭并提交：

1. **候选可激活**（v3 的空 snapshot + 26 文件子集缺陷）→ schema v4 + 父链继承
   + 生成期严格校验 + harness fail closed；
2. **归档可自证**（提交式篡改/同步改 manifest 无法被内部检查发现）→ 外部回执
   + `--expect-receipt` witness + manifest 钉值，7 类负向用例全部拦截；
3. **增量索引 re-export 陈旧边**（probe `incremental_equals_full=False`）→
   变更文件 IMPORT 目标集检测回退全图重建，probe/digest 全等，性能无回退。

唯一未清零项：tests/graph 组 16 例**历史遗留**失败（基线 38e395a 与 HEAD 双侧
复跑逐一相同，与本轮改动无关），已按任务书 T10 入档
`phase30-known-failures.json`（status=pre-existing，blocking=false），不使用
skip/xfail，不计入"通过"。任何相关结论均为 **"3284 passed, 16 known
pre-existing failures"** 口径（unit 3009 + integration 125 + graph 250）。

## 2. P0 关闭证据

### P0-A 候选可激活（G1/G2）

| 要求 | 结果 |
|---|---|
| v4 prompts 三角色非空、继承父 snapshot | `snapshot.json` prompts = {coder 361, planner 1823, reviewer 496}（经 v3→v2 链继承），experiences=1；`snapshot_strict_roles` 检查过 |
| 只有 26 变更文件、无 agents/*.py 的 full 候选被拒绝 | `test_v4_full_rejects_changed_files_only_subset`（IncompleteSourceManifestError: missing inherited files）；verifier `source_manifest_full_coverage` 对 91 个继承文件复核 |
| delta 无 parent 被拒绝 | `test_v4_delta_without_parent_rejected` |
| full 缺 agents/coder.py 被拒绝 | `test_v4_root_full_missing_agent_source_rejected` |
| 空 prompts 生成中止 | `test_v4_strict_empty_prompts_abort_generation`（严格校验 ValueError，目录零残留） |
| harness fail closed | `test_fail_closed_when_selected_candidate_missing_prompts`：SnapshotIdentityError 含候选名/缺失角色/snapshot 路径，不回退 deps 默认 prompt；`test_fail_closed_when_one_role_prompt_missing`（删单角色即拒）；`test_fail_closed_when_active_snapshot_unreadable` |
| 无候选内置路径不误伤 | `test_builtin_path_without_pointer_is_untouched`（load_selection 不被调用） |
| 激活 dry-run 用候选 prompt | `test_activation_dry_run_uses_candidate_prompts`：Fake agents 加载 v4 后 system_prompt = 候选值（"candidate coder prompt"），非代码默认 |
| validate_snapshot({}) 失败 | `test_validate_snapshot_strict_and_empty` |
| 真实 configs/harness/active.json 始终不存在 | 轮末确认：`ls configs/harness/active.json` → No such file |

测试：`tests/unit/test_phase30_archive_v4.py` 11 例 +
`tests/unit/test_phase30_harness_failclosed.py` 6 例，全绿。

### P0-B 归档自证（G3/G8）

回执：`configs/harness/receipts/phase30-candidate-v4.json`（候选目录之外），
12 项具名检查（`receipt_schema` / `receipt_witness` / `receipt_commit_ancestor`
/ `receipt_candidate_files` / `receipt_candidate_tree_self` /
`receipt_candidate_tree_current` / `receipt_source_ancestry` /
`receipt_source_hashes_blobs` / `receipt_candidate_record` /
`receipt_status_pending` / `receipt_parent_present` / `receipt_diff_roundtrip`
/ `receipt_snapshot_strict` / `receipt_not_active` / `receipt_historical_manifest`）。

正向：`verify --all --ignore-raw-drift --receipt <回执> --expect-receipt
b9a24285bd0056da5a2aa20c3dab39516ea16a4c6186e16b27f973ed99d02c7d` → **exit 0**，
`witness=verified`，`historical_integrity=ok`（log：
`.codex-pytest-tmp/phase30/verify-final.log`）。

负向矩阵（全部先篡改后 commit，逐条断言具体检查名，8 例全绿，
`tests/unit/test_phase30_archive_receipt.py`）：

| 篡改 | 退出码 | 命中字段 |
|---|---|---|
| 旧候选 sources.json +1 字节、同步改 manifest、commit | ≠0 | `receipt_historical_manifest` |
| 篡改候选文件后 commit | ≠0 | `receipt_candidate_tree_current` + `receipt_candidate_files` |
| source_commit 改 40 个 0（同步 witness） | ≠0 | `receipt_source_ancestry` + `receipt_source_hashes_blobs` |
| status=active 后 commit | ≠0 | `candidate_payload_sha256`（seal 失效） |
| 删除父候选后 commit | ≠0 | `receipt_parent_present` + `parent_integrity_matches` |
| 篡改候选 + 同步 receipt、witness 不变 | ≠0 | `receipt_witness`（mismatch） |
| 无 witness 跑验证 | 0 | `historical_integrity=internal-only`（≠ok） |

witness=none 时输出 `internal-only`，永不输出 `historical_integrity=ok`；
真实 configs/harness 未被任何负向测试改写（全部临时夹具）。

### P0-C 索引 re-export（G4）

先红后绿：`test_phase30_index_reexport.py` 5 例中 3 例在修复前失败
（retarget / 两层链 / 目标迁移），修复后 5/5 绿。

## 3. 测试结果

| 组 | 命令 | 结果 |
|---|---|---|
| tests/unit | `-m "not docker and not llm"` | **3009 passed, 2 skipped**（os.mkfifo 平台限制，预先存在） |
| tests/integration | 同上 | **125 passed, 1 deselected**（docker）；首跑发现的 1 例 CLI dry-run 失败属本轮范围，已修（67e6428）并复跑 5/5 |
| tests/graph | 同上 | **250 passed, 16 failed**＝16 例已知历史失败（与基线逐节点相同），13 deselected |
| 前端 | typecheck / eslint / vitest / build | 全过（vitest 14 文件 50 例）；web 零改动（`git diff --name-only 6f023fa..HEAD -- web` = 0），页面 E2E **条件跳过并如实记录**，未伪装为已跑 |

去重口径按命令分组；无 skip/xfail 弱化；无超长 timeout。

## 4. 索引结果

| 项 | 前 | 后 |
|---|---|---|
| re-export probe（1 跳/2 跳） | `incremental_equals_full=False` | **True / True** |
| 452 文件 digest 等价 | — | 增量 == 全新全量（sha256 逐字节一致；452/3234/9581 行） |
| body-only fast path | 生效 | 仍生效（`test_body_only_change_still_uses_fast_path`：替换集 < 全图） |
| cold | 9.0568s | 7.9774s（−11.9%） |
| sameproc_nochange | 1.3804s | 1.2348s（−10.5%） |
| sameproc_change | 5.1318s | 4.8434s（−5.6%） |

局限：进程内计时（Phase29 基线为子进程隔离），只可比趋势；合成仓库结论不外推
大型真实仓库。IMPORT 绑定检查代价 = 变更文件符号数的 `out_edges` 索引查询，
观测无回退；若未来大仓出现回退，优化入口为"按文件批量取存储 IMPORT 边"的
store 方法（见 phase30-index-profile.json）。

## 5. 候选与回执

| 项 | 值 |
|---|---|
| candidate | `configs/harness/candidates/phase30-candidate-v4/`（4 文件） |
| candidate_commit | `ffa97d983e35618443b6c79d22bccdb6fa96fa79` |
| source_commit | `8e1f1f47467427b97aa9e02c076e803500d9f102` |
| diff_base_commit | `b17019beb817b92b1fbb3da879774a231d69219d`（= v3 source_commit） |
| parent | `phase29-candidate-v3`（v3 文件未动，标注 superseded by v4） |
| source_files | 96 = source_hashes 键数 = 实际（91 继承 v2∪v3 + 11 本轮） |
| status | pending；active 指针不存在 |
| candidate_tree_sha256 | `8f452b7702fd85eb327e4ccd7131ca1a5decba21692f8d9a3214ab4b126e3f69` |
| receipt | `configs/harness/receipts/phase30-candidate-v4.json`；witness sha256 `b9a24285bd0056da5a2aa20c3dab39516ea16a4c6186e16b27f973ed99d02c7d` |
| historical manifest | 40 文件（含 v4），钉值 `5ec24891a9dc02c656c78d0fa7b28b0e0167e23f12b43d1a1313151231d184e2` |
| 校验 | 单候选 22/22；verify --all exit 0（witness=verified / historical_integrity=ok） |

tree hash 规则：候选目录内 4 文件按路径排序，逐项 `path + NUL + file_sha256`
拼接后取 SHA-256；不含 receipt 自身（实现在
`scripts/phase28_archive_verify.py::candidate_tree_sha256`）。

## 6. 修改与提交

| # | 提交 | 内容 |
|---|---|---|
| 1 | `8e1f1f4` | feat：schema v4 生成器 + 严格校验 + harness fail closed + 校验器 receipt/witness + re-export 修复 + 测试（11 文件，+2830/−402） |
| 2 | `ffa97d9` | feat：phase30-candidate-v4（仅 4 个候选文件，无源码混入） |
| 3 | `255f13c` | docs：回执 + 重生成 manifest + phase29-corrections.md（C1–C6）+ 占位填充 |
| 4 | `c939cbc` | fix：回执重钉 LF 归一后的 manifest 字节（255f13c 的钉值先于归一，如实记录） |
| 5 | `6e466a8` | docs：archive 模块 docstring 反映 v4 |
| 6 | `67e6428` | test：CLI 契约测试显式钉 v3 + 新增 v4 默认拒绝测试 |
| 7 | 本提交 | docs：phase30 final report / runtime results / static checks / known-failures / index-profile |
| 8 | 交付回执提交 | docs：phase30-delivery-receipt.json（记录第 7 笔哈希，避免 SELF 自引用） |

tracked 收口时干净；未跟踪 174 项全部保留（历史证据与用户文件，0 删除——
按任务书口径写"174 项已分类，0 项可安全删除"，不写"清理完成"）。

## 7. Phase29 文档与证据修正（T09/T10/T11）

- `phase29-corrections.md` 六条：C1 裸 verify 退出码、C2 PENDING_T18 真实哈希、
  C3 receipt SELF→`6f023fa`、C4 未格式化 6 文件清单（原漏 2 个 phase19 测试）、
  C5 v3 superseded、C6 B2 16 例如实记录；全部保留"首次记录/复核/修复后"入口。
- `phase29-final-report.md` 追加 "Post-review correction" 小节（不改写原结论）。
- B2：16 节点在基线副本仓与 HEAD 双侧复跑（16 failed / 13 passed 完全一致），
  入档 `phase30-known-failures.json`；无 skip/xfail；本轮未修复（失败域为
  graph closure 预算/预留语义，非本轮 P0）。
- 清理口径修正：删除 0 文件，"183→174 项已分类，0 项可安全删除"。

## 8. 仍待验证的真实能力（与 phase29 §12 一致，本轮未新增未验证项）

1. 真实 provider 流式 deadline/usage（0 请求/0 费用）；
2. Bedrock/TLS-CONNECT/池化取消；
3. Docker/官方 Sandbox/官方 Harness；
4. 大型真实仓库性能（test50/test300 级授权）。

## 9. 下一阶段条件

1. **v4 激活前置**：v4 保持 pending；激活 = `rsi.select --allow-pending` 写
   active.json 后，harness 将从候选 snapshot 注入三角色 prompt；任何角色缺失
   都会 SnapshotIdentityError fail closed（本轮已验证）。正式启用仍需按
   phase29 §14 的比较证据流程（promotion_readiness）。
2. **verify witness 使用**：外部持有
   `b9a24285bd0056da5a2aa20c3dab39516ea16a4c6186e16b27f973ed99d02c7d`
   （receipt 文件字节哈希）；此后每次
   `verify --all --receipt configs/harness/receipts/phase30-candidate-v4.json
   --expect-receipt <sha>` 校验整链。注意：重生成 historical-baseline.json
   会使旧 witness 失效（这是设计语义），届时需发布新回执。
3. **索引后续优化入口**：`_import_bindings_changed` 的存储边读取按变更符号
   逐个 `out_edges`；如需批量，加 `EdgeStore.list_for_sources` IN 查询；
   `_MAX_INHERITANCE_HOPS=8` 为继承链上界。

## 10. 验收清单（G0–G9）

### G0 基线 — 全过
- [x] HEAD `6f023fa`、tracked 干净、v3 pending、无 active：T00 记录（ledger.md）
- [x] 未跟踪证据保留、0 清理
- [x] 真实模型请求 0、费用 0

### G1 schema/生成器 — 全过
- [x] full/delta、父继承、完整源集测试通过（11 例 + 集成 5 例）
- [x] v1/v2/v3 旧候选继续可验证（phase29 48 例全绿；verify --all 10 版本 0 失败）
- [x] 提交解析为全 sha、拒标签；脏树走临时共享 clone（用户改动不动）

### G2 激活门禁 — 全过
- [x] 空 prompts 生成失败；harness fail closed（候选名/缺失角色/路径入错误信息）
- [x] dry-run 断言 agent prompt 来自候选
- [x] 真实 active.json 始终不存在

### G3 归档自证 — 全过
- [x] 正向 verify（witness）exit 0
- [x] 7 类负向用例全部非 0 且错误指明字段
- [x] 真实 configs/harness 不被负向测试改写

### G4 索引正确性 — 全过
- [x] re-export probe 增量==全量（1 跳/2 跳）
- [x] 452 文件 digest 逐字节等价；body-only fast path 保持
- [x] 性能无回退；局限如实记录

### G5 文档/证据 — 全过
- [x] 无 PENDING 残留（phase29 占位已填，phase30 无占位）
- [x] 6 文件清单三处一致（corrections C4 / phase29-static-checks / phase30-static-checks）
- [x] B2 已知失败如实记录（不写"全过"）

### G6 回归 — 全过
- [x] unit 3009 / integration 125 / graph 250+16 已知单列
- [x] 无重叠计数冒充覆盖（按命令分组去重）
- [x] 无 skip/xfail 弱化（2 skip 为 os.mkfifo 平台预先存在）

### G7 静态 — 全过
- [x] AST 952 文件、ruff check 0、本轮文件 format 0、mypy 9 文件、tsc/eslint、git diff --check 干净

### G8 候选 — 全过
- [x] v4 pending、parent=phase29-candidate-v3 准确、无 active、receipt/witness 一致
- [x] v3 保留并标注 superseded；旧候选与旧证据 0 改动

### G9 交付 — 全过
- [x] 本地提交链完成（8 笔）；不 push、不建 PR
- [x] 真实模型请求 0、费用 0
- [x] 未完成项均有证据与原因（§8、not_run 清单）
