# Phase28 归档 schema v2（契约 A 实现说明）

T02 交付文档。生成器：`src/codepilot/archive.py`（`create_candidate`），CLI：`scripts/phase28_archive.py`，测试：`tests/unit/test_phase28_archive.py`。本文冻结 v2 目录的字段与哈希规则，供 T06 校验器与 T09 候选生成引用。

## 背景

`rsi.archive_candidate()` 写出的 v1 `candidate.json` 不含 snapshot/diff 完整性字段；v1 的 `snapshot_sha256` 是 snapshot.json 文件字节哈希，而历史 v2–v5 声明的是 canonical JSON 哈希且规则从未标注（`docs/notes/phase26-final-archive-integrity.json`、phase27 limitations 第 8 条）。v2 把每条哈希规则与值并列写明，删除冒充源码版本的 `code_revision`，并在生成时自证 diff 可反向应用。生成路径版本化（`created_from`），不改 `rsi.py` 现有 API。

## 目录布局

`<archive_root>/candidates/<version>/` 下恰好 4 个文件；绝不创建 `active.json` / `active.next.json`（指针只能由 `rsi.select` 显式创建）：

| 文件 | 内容 |
|---|---|
| `snapshot.json` | policy 快照，`schema_version=1`，必须通过 `rsi.validate_snapshot` |
| `candidate.json` | 溯源与完整性记录，`schema_version=2` |
| `sources.json` | 仓库相对路径 → LF 规范化源码文本 |
| `candidate.diff` | `git diff --binary <diff_base_commit> -- <paths>` 的原始字节 |

## 哈希规则（全部显式标注）

| 规则名 | 定义 |
|---|---|
| `file_bytes_sha256` | 文件在磁盘上的字节 SHA256（`snapshot_file_sha256`、`diff_sha256`、`raw_worktree_hashes`） |
| canonical JSON | `sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8"))`，与 `rsi._json_hash` 同规则（`snapshot_canonical_sha256`） |
| LF 规范化 | 字节按 UTF-8 严格解码后，universal newline 归一为 `\n`，再以 UTF-8 编码取 SHA256（`source_hashes`） |

所有 JSON 写出确定性：`json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n"`，UTF-8 无 BOM。v2 字段集合不含时间戳，同一 git 状态重复生成字节级一致（测试 `test_generation_is_byte_deterministic`）。

## snapshot.json（schema_version=1，不变）

字段：`schema_version=1`、`version`、`parent`、`status="pending"`、`instruction`、`prompts`、`experiences`。`prompts` 用 `rsi.extract_prompts` 同逻辑从 LF 规范化源码提取（coder/planner/reviewer 三个角色；角色文件按路径后缀 `agents/<role>.py` 解析，因此 `src/codepilot/agents/<role>.py` 这类带前缀的清单键同样命中，多个同名时取最短键；某个角色文件不在源文件清单中时该角色缺省，不报错）。契约 A 的生成器输入不含 policy 载荷，因此默认 `instruction=""`、`experiences=[]`；模块级保留 `instruction` / `experiences` 关键字参数供程序化调用，CLI 不暴露。

## candidate.json（schema_version=2）字段表

| 字段 | 类型/值 | 说明 |
|---|---|---|
| `schema_version` | `2` | |
| `version` / `parent` | str / str·null | parent 为空（根候选）时是 `null` |
| `status` | `"pending"` | |
| `created_from` | `"phase28_archive"` | 生成器标识 |
| `snapshot_ref` | `"snapshot.json"` | |
| `snapshot_hash_rule` | `"file_bytes_sha256"` | 声明 `snapshot_file_sha256` 的规则 |
| `snapshot_file_sha256` | hex | snapshot.json 写出后的文件字节 SHA256；先写 snapshot.json，再计算，再写 candidate.json |
| `snapshot_canonical_sha256` | hex | snapshot.json 解析后 dict 的 canonical JSON SHA256 |
| `diff_base_commit` | git sha | **diff 基线唯一权威字段**；`code_revision` 在 v2 删除 |
| `source_commit` | git sha | 被固化的源码提交（生成前必须已存在，两个 sha 都经 `git rev-parse --verify <sha>^{commit}` 校验） |
| `diff_sha256` | hex | candidate.diff 字节 SHA256 |
| `source_hashes` | map<path, hex> | LF 规范化文本 SHA256 |
| `raw_worktree_hashes` | map<path, hex> | 工作区原始字节 SHA256 |
| `source_files` | int | 文件数 |
| `normalization` | 固定串 | `"UTF-8 LF normalized; raw worktree hashes recorded separately"` |
| `parent_integrity` | object·null | 见下节 |
| `dirty_worktree` | bool | 仅在显式 `allow_dirty` 放行脏树时为 `true`；干净树为 `false` |
| `dirty_files` | list[str] | 仅 `dirty_worktree=true` 时出现；`git status --porcelain --untracked-files=no` 的原始行 |

## parent_integrity（仅记录，不判定）

```
{
  "version": "<parent>",
  "snapshot_file_sha256_actual":     "<parent snapshot.json 实际文件字节哈希>",
  "snapshot_canonical_sha256_actual": "<parent snapshot.json canonical JSON 哈希>",
  "declared_snapshot_sha256":        "<parent candidate.json 声明值，无则 null>",
  "declared_snapshot_sha256_field":  "<'snapshot_file_sha256' | 'snapshot_sha256' | null>",
  "file_bytes_consistent":           true|false|null,
  "canonical_consistent":            true|false|null
}
```

声明值按 `snapshot_file_sha256`（v2）→ `snapshot_sha256`（v1）优先级取第一个字符串字段；同时给出与两种规则的比对结果，因为历史档案的声明规则未标注。parent 必须存在 snapshot.json，否则生成失败；声明值不一致**只记录、不阻断**。

## sources.json 与 candidate.diff

- `sources.json`：`{<repo 相对路径>: <LF 规范化文本>}`，与 `source_hashes` 一一对应。路径键统一 POSIX 正斜杠；拒绝绝对路径、`..` 与解析后越出仓库根的路径。
- `candidate.diff`：`git diff --binary --no-color --no-ext-diff --no-textconv --no-renames <diff_base_commit> -- <paths>` 的原始字节（契约命令外加确定性 flags，防止外部 diff 驱动/重命名检测改变输出）。diff 与工作树状态对齐，生成后用 `git apply --check -R --whitespace=nowarn`（stdin 送入字节，`--check` 不写工作区）验证可反向应用。**空 diff 视为平凡可逆，跳过该检查**（`git apply` 对空输入报 "No valid patches in input"，退出码 128）；空 diff 在 summary 里以 `diff_bytes: 0` 显式可见。

## 生成器行为

输入：archive root、source 仓库根（可注入，git 一律 `git -C <root>`）、version、parent、diff_base_commit、source_commit、源文件相对路径列表（通用，不硬编码 64）。拒绝条件（全部在写盘前或带清理地失败）：

1. version 标识非法或目标目录已存在 → 拒绝，绝不覆盖；
2. parent 给定但其 snapshot.json 缺失 → 失败；
3. `diff_base_commit` / `source_commit` 不能解析为提交 → 失败；
4. tracked 工作树不干净（untracked 文件不算）默认拒绝；`allow_dirty=True` 放行并记录 `dirty_worktree=true` + `dirty_files`；
5. 任一源文件缺失（或非 UTF-8）→ 失败，列出全部缺失路径；
6. 生成完成后 diff 反向应用失败 → 报错并删除本次创建的半成品目录（只删这个目录）。

git 调用强制 `-c core.autocrlf=false -c core.longpaths=true`、`GIT_TERMINAL_PROMPT=0`，并剥离 `GIT_DIR`/`GIT_WORK_TREE` 等会改变"当前仓库"的环境变量（与 `codepilot.repo.gitcmd` 的不变量一致，保证 diff 字节跨机可复现）。CLI 另有 `--dry-run`：完整执行全部校验与哈希计算（含反向应用检查）但不落盘；本轮（T02）只演示 dry-run，真正的 `phase28-candidate-v2` 由 T09 在最终源码提交之后生成。

## v1 → v2 字段映射

| v1（phase27-candidate-v1 为例） | v2 |
|---|---|
| `code_revision`（冒充源码版本，实为 diff 基线） | **删除**；由 `diff_base_commit` 承担，语义唯一 |
| `code_snapshot.base_commit` | 提升为顶层 `diff_base_commit` |
| `code_snapshot.sources_ref` | 固定文件 `sources.json` |
| `code_snapshot.source_hashes`（LF 规则未标注） | 顶层 `source_hashes`，规则写入 `normalization` |
| `code_snapshot.raw_worktree_hashes` | 顶层 `raw_worktree_hashes` |
| `code_snapshot.normalization` | 顶层 `normalization` |
| `code_snapshot.diff_sha256` | 顶层 `diff_sha256` |
| `snapshot_sha256`（规则未标注，v1 实为文件字节哈希） | `snapshot_file_sha256` + `snapshot_hash_rule` 显式声明；canonical 值单列 `snapshot_canonical_sha256` |
| `parent_integrity.snapshot.*` | `parent_integrity`（见上） |
| `snapshot` 内嵌在 candidate.json 中 | 外置为独立 `snapshot.json`（`snapshot_ref` 指向） |

## 决策与边界

- `instruction=""`、`experiences=[]` 为默认值：契约 A 的输入清单不含 policy 载荷，policy 内容的继承/变更属 T09/主控决策，生成器不擅自携带父代经验。
- 生成前校验两 sha 可解析，但不校验 `source_commit` 与 `diff_base_commit` 的祖先关系、也不判定 diff 是否为空——候选 diff 语义由 diff 基线与路径清单决定，判定留给校验器（T06）。
- 历史候选（phase26-*、phase27-*）与本生成器无交互：只读其 snapshot.json/candidate.json 计算 `parent_integrity`，从不修改。
- 验收：`pytest tests/unit/test_phase28_archive.py`（15 项，覆盖双哈希复算、CRLF 源、重复版本拒绝、缺源、脏树默认拒绝/放行记录、反向应用通过/失败清理、空 diff、无 active 指针、v1→v2 字段映射、parent_integrity 记录不判定、CLI 三态与 `--source-list` 解析）。
