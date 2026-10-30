# Phase29 归档 schema v3 设计（T02 产出）

范围：`codepilot.archive.create_candidate`（生成器）与 `scripts/phase28_archive_verify.py`（校验器）的 v3 合同。
父版本：schema v2（`docs/notes/phase28-archive-schema.md`，phase28-candidate-v2 在档，不迁移不改写）。

## 1. 设计原则

1. **来源即提交**：`git_commit` 模式下，一切源码事实从 git blob 读取，工作区只提供"干净"保证，不提供内容。
2. **三角自洽**：`source_commit` ↔ `diff_base_commit` ↔ `candidate.diff` ↔ `sources.json` 四者互相可机械验证；任一被篡改至少落在一个具名检查上。
3. **防误改不防伪造**：`candidate_payload_sha256` 是完整性校验和，不是数字签名；外部锚点仍是 git 提交链与交付回执。
4. **双模式显式化**：`worktree` 模式不冒充 git 来源；`allow_dirty` 只在 worktree 模式合法。

## 2. 字段表（candidate.json，schema_version=3）

| 字段 | 类型 | 模式 | 语义 |
|---|---|---|---|
| `schema_version` | 3 | 两者 | 固定 3 |
| `version` | str | 两者 | 候选版本标识，等于目录名 |
| `parent` | str\|null | 两者 | 父候选版本 |
| `status` | "pending" | 两者 | 生成器只产 pending |
| `created_from` | str | 两者 | 生成器标识，保持 `phase28_archive`（同一代码路径升级，非新系统） |
| `source_origin` | "git_commit"\|"worktree" | 两者 | 来源模式声明；缺失即校验失败（检查名 `source_origin_declared`） |
| `source_commit` | sha1\|null | git_commit 必填；worktree 必为 null | 干净模式下来源提交；worktree 模式不宣称 git 来源 |
| `diff_base_commit` | sha1 | 两者 | diff 基准提交；git_commit 模式必须是其祖先（`source_commit_ancestor`） |
| `snapshot_ref` / `snapshot_hash_rule` / `snapshot_file_sha256` / `snapshot_canonical_sha256` | 同 v2 | 两者 | 与 v2 语义一致（file_bytes + canonical 双哈希，规则显式标注） |
| `diff_sha256` | sha256 | 两者 | candidate.diff 文件字节哈希 |
| `source_files` | int | 两者 | 必须等于 `source_hashes` 键数（`source_files_count`） |
| `source_hashes` | map[path→sha256] | 两者 | **LF 规范化文本**哈希；git_commit 模式文本来自 `git cat-file blob source_commit:<path>`（`source_commit_blob`）；worktree 模式来自工作区文件 |
| `base_hashes` | map[path→sha256] | 两者 | `git cat-file blob diff_base_commit:<path>` 的 LF 文本哈希（`base_commit_blob`）；diff 正向应用的预期起点 |
| `normalization` | str | 两者 | "UTF-8 LF normalized; raw worktree hashes recorded separately" |
| `parent_integrity` | object\|null | 两者 | 同 v2：比较性记录，不作为判定依据 |
| `dirty_worktree` | bool | 两者 | true 仅当 source_origin="worktree" 且树脏（`origin_dirty_consistency`） |
| `dirty_files` | list[str] | 仅 worktree 且树脏 | 同 v2 |
| `raw_worktree_hashes` | map[path→sha256] | 仅 worktree | 工作区原始字节哈希（漂移检测）；git_commit 模式不携带（来源是 blob，非工作区） |
| `candidate_payload_sha256` | sha256 | 两者 | 对 candidate.json 去掉本字段后的 canonical JSON（sort_keys + ensure_ascii=False）哈希；防误改，字段任一变化必须导致 `candidate_payload_sha256` 失败 |
| ~~`code_revision`~~ | — | — | v2 已删除，v3 保持删除 |

路径键全部为 POSIX 仓库相对路径；不存在的路径在生成期报 `MissingSourceError`。

## 3. 生成器流程（v3）

1. 解析 `source_commit`、`diff_base_commit`（`rev-parse --verify <sha>^{commit}`）。
2. `git_commit` 模式强制 `git merge-base --is-ancestor diff_base source_commit`，失败报 `ArchiveError`（消息含两提交与"ancestor"关键词）。
3. 工作区状态：`git status --porcelain --untracked-files=no`。`git_commit` 模式要求干净（否则 `DirtyWorktreeError`）；`worktree` 模式记录 dirty_files。
4. `git_commit` 模式：逐文件 `git cat-file blob source_commit:<path>` 取字节 → LF 规范化 → sources.json / source_hashes；`diff_base_commit` 同法 → base_hashes。worktree 模式：沿用 v2 工作区读取（sources + raw_worktree_hashes），base_hashes 仍从 diff_base blob 取。
5. diff：`git diff --binary --no-color --no-ext-diff --no-textconv --no-renames diff_base source_commit -- <paths>`（git_commit 模式，commit-vs-commit）；worktree 模式沿用 v2（diff_base vs 工作区）。
6. 写 snapshot.json / sources.json / candidate.diff / candidate.json（四文件，无 active 指针）。
7. 反向应用检查：临时目录预写 source_commit blob 字节（worktree 模式：工作区字节），`git apply -R --check` candidate.diff，再逐文件比对结果与 base blob LF 哈希。
8. 正向应用检查：临时目录预写 diff_base blob 字节，`git apply` candidate.diff，逐文件 LF 比对与 sources.json 一致。
9. 任一步失败：删除本次创建的候选目录，不留半成品。dry_run 执行全部计算与检查（apply 检查本身只写临时目录），不落盘。

## 4. 校验器检查清单（v3 新档，schema_version==3）

按序执行，全部具名（新增粗体）：

1. `version_matches_directory`、`snapshot_hash_rule_declared`、`snapshot_ref_fixed`（沿用 v2）
2. `snapshot_file_sha256`、`snapshot_canonical_sha256`、`diff_sha256`（沿用 v2）
3. **`candidate_payload_sha256`**：复算 canonical JSON（去自身字段）——拦截 status/parent/version/source_commit 等一切 candidate.json 内容篡改
4. **`source_origin_declared`**：值合法且与其余字段一致（git_commit ⇒ source_commit 非空、无 raw_worktree_hashes；worktree ⇒ source_commit 为 null）
5. **`origin_dirty_consistency`**：git_commit 模式 ⇒ `dirty_worktree=false` 且无 `dirty_files` 字段；worktree 模式 ⇒ `dirty_files` 存在时 `dirty_worktree` 必为 true（该检查只约束记录字段，不检查当前工作区——v3 的来源是提交，生成后的工作区演进不影响结论）
6. **`source_commit_resolves`**：source_commit 可解析为 commit
7. **`source_commit_ancestor`**：`merge-base --is-ancestor diff_base source_commit`
8. **`source_commit_blob`**（git_commit）：逐文件 `git show source_commit:<path>` LF 哈希 == source_hashes
9. **`base_commit_blob`**：逐文件 `git show diff_base:<path>` LF 哈希 == base_hashes
10. **`diff_forward_apply`**：临时目录预写 diff_base blob → `git apply` → 逐文件 LF 比对 sources.json
11. `diff_reverse_apply`：临时目录预写 source_commit blob（worktree 模式：当前工作区）→ `git apply -R --check` → 逐文件比对 base 内容
12. `source_hashes` / `source_files_count`（沿用：sources.json 与 source_hashes 键集一致、重算一致）
13. `raw_worktree_hashes`（仅 worktree 模式要求存在并比对；git_commit 模式不检查）
14. `parent_integrity_matches`、`not_active`（沿用 v2）
15. **`historical_baseline`**：若 `<archive_root>/candidates/historical-baseline.json` 存在，逐文件比对当前字节哈希；不匹配（含"改后提交"）即失败（T05）

失败输出含检查名与证据摘要，退出码 1；v2 及更早档案继续走既有分类路径（`classify()` 按 schema_version 分派），历史候选不受 v3 影响。

## 5. 迁移与拒绝条件

- **不迁移**：v1/v2 候选按原样保留、按原规则校验；v3 只影响新生成。
- **生成器拒绝**：空 source_files；版本目录已存在；非法路径（绝对/逃逸/重复）；git_commit 模式下脏树、错误 commit、非祖先 diff_base、blob 缺失、非 UTF-8；worktree 模式未显式声明；`allow_dirty=True` 且 `source_origin="git_commit"`（显式报错，防止旧调用习惯绕过模式声明）。
- **校验器拒绝**：candidate_payload_sha256 不匹配；source_origin 缺失/非法；git_commit 模式 blob/祖先/正向应用不一致；base_hashes 不一致；historical_baseline 不一致。

## 6. 与 6 问题的对应

- I1：§3.1-2、§4.3-10（来源绑定、祖先、blob、正向应用、自哈希）。
- I2：§4.15（historical_baseline，T05 落 manifest）。
- I4：§4 清单即负向用例的断言目标（每条测试断言到检查名）。
- I3：v3 是唯一受支持生成路径（T06 收口 v1）。
