# Phase31 archive schema v5

`CANDIDATE_SCHEMA_VERSION=5`。schema 3/4 保持可读与原生成规则；新规则只用于 v5。
v5 只接受 git_commit 来源，拒绝调用者的 source_files / --source-list，由提交与父候选推导。
CLI 接受 --candidate-schema-version 5（--schema-version 的同义参数）。

源码范围集中定义于 `codepilot.archive.SOURCE_SCOPE`：src/、scripts/、tests/、web/src/、
configs/harness/、pyproject.toml、web/package.json；candidates/ 与 receipts/ 永远排除。
父集中范围外文件写入 excluded_files，包含具体原因；docs/notes/、临时文件不得进入源码哈希。
`git diff --name-status -z -M diff_base source_commit` 使用 NUL 分隔，保留特殊路径。
changed_files 是范围内 A/M 与 R 的新路径；deleted_files 是所有 D 与 R 旧路径。
范围外的变更路径（重命名两端分别判断）记录在 scope_excluded_changed_files。
Git 的 T（文件类型变化）作为变更路径处理；未支持的状态直接拒绝。

令 P 为父候选的解析源集，D 为 deleted_files，E 为 excluded_files 的路径，
C 为 changed_files，R 为三个固定 required_sources：

```
full = (P - D - E) ∪ C ∪ R
delta = C ∪ (R - P)
delta resolved = (P - D - E) ∪ delta
```

schema>=4 full 的解析在该父节点终止；v5 delta 按 tombstone 和排除项递归解析；
旧 subset schema 使用有界父链累计集。源集最多 8 跳，环或超限失败。
required_sources 固定为 src/codepilot/agents/{coder,planner,reviewer}.py。
删除任何 required source 都拒绝生成；普通删除从 full 与解析集移除。
重命名等价于旧删新增，不增加 rename 字段。
source_hashes 恰好等于实际 sources.json 的 LF UTF-8 哈希；full 存完整集，delta 只存 delta。
source_manifest.resolved_source_files 记录解析后文件数量。

candidate.diff 覆盖完整解析集及 tombstone（delta 也如此），base_hashes 绑定同一组路径。
正向逐字节重建 source_commit 的完整源集并检查删除；反向逐字节恢复 diff_base。
归档源文本的跨平台哈希使用 LF。临时 apply 目录初始化独立 git，防止 TEMP 位于
checkout 子目录时 git apply 静默忽略补丁。工作区内容不参与 git_commit 的源文本。

父 snapshot_file_sha256 必须等于父文件字节。schema>=4 无豁免；只有 schema<4 且
位于代码中的显式 LEGACY_PARENT_ALLOWLIST 才能带 legacy_parent_reason 继续。
coder/planner/reviewer 分别寻找父链中第一个非空 prompt；instruction/experiences 各自取最近非空值。
snapshot 继承最多读取 8 个节点；在此界内检测环。若到界时还有值未解析则拒绝；
若全部值已在界内找到，则无需读取更远的历史节点。真实 v4 的所有值均在最近父节点已给出。
本轮归档的角色源码中非空 prompt 覆盖继承值，最后执行 strict snapshot 验证。

receipt 与 candidate.json 的 version/source_commit/diff_base_commit 逐字段交叉校验，
candidate_files 恰好四项，磁盘/候选提交 blob/树哈希同时匹配。source_commit 必须为
candidate_commit 祖先，diff_base 必须为 source_commit 祖先。v5 status 必须 pending，
parent 存在且严格哈希通过，active.json/active.next.json 必须不存在。

v5 receipt 必须包含 manifest_commit、manifest_blob_sha256，manifest_commit 为 HEAD 祖先。
从该提交读取 historical-baseline.json blob，两个 manifest 哈希都绑定其字节，并比对当前磁盘。
v4 无 manifest_commit 的旧 receipt 只报告 manifest_binding=current_worktree，不宣称提交绑定。
生成器拒绝覆盖；显式 --replace 必须提供 --reason，记录旧 receipt 哈希与原因。
生成器不生成 witness；外部 witness 是 receipt 字节 SHA-256，不写入 receipt 自身。

schema 3 的字节检查仍执行，但标为 legacy_non_strict，reasons 包含 schema<4，
空角色 prompt 另加 empty_role_prompts；文档标注 superseded，不修改历史文件。
裸 verify --all 默认只以归档字节验证旧 worktree 来源；raw drift 注释为
legacy_worktree_drift。只有 --strict-raw-drift 显式要求时漂移才使退出码失败。
没有外部 witness 时 historical_integrity=internal-only，有 witness 且全部一致才是 ok。

## 16 项失败中的旧期望与现行语义

Phase26 提交 748ad2f 已增加 low_yield>=6 时一次 coder_relocalize，只读检查后继续编码。
旧 Phase11/12 脚本只提供 coder 回复，导致 AssertionError（并非预算/闭环产品路径失败）。
本轮通过显式 coder_relocalize_replies 补齐读请求；30 次工具、总模型记账、闭环、
全局 stop reason 和不重复收费的断言全部保留。统计 coder 请求时包含只读子用途。

collection 归因要求 traceback 指向被改文件；没有帧的 COLLECTION_ERROR 是 UNKNOWN
基础设施归因。原测试声称“补丁导致收集错误”却没有提供任何代码帧，本轮补实际
src/pkg/core.py:2 SyntaxError 证据，仍要求进入诊断和修复。

现行 select_target_tests 先保留已授权 Planner 目标，再用 reproduction/impact/baseline/related。
旧复现用例“复现一定替换已有目标”的期望已过时；本轮明确断言复现校验仍执行，
最终目标来源是 planner，专用临时测试仍不进入交付补丁。
同理空 Planner 清单不再等于无目标：终止用例必须构造确实没有既有测试的仓库，
才能验证 TARGET_TEST_UNAVAILABLE，保留失败状态及 never executed 原断言。

RegressionRunner 在 L2 已完整覆盖全部授权 baseline 时以该证据同时满足 L3，不重复执行。
恢复的计费计数应按真实 scope 执行次数断言，并同时核对 complete、L3 级别和恢复不重收费。
完整复现、实际断言值和最终证据见 phase31-known-failures.json。

旧 Phase28/29 的 CLI 漂移用例改用显式 --strict-raw-drift 验证 raw 失败，
同时断言归档 diff 的正反向验证仍有效。默认路径另断言 exit 0 和 drift 注释。
这对应本轮明示的新 CLI 合约；不再将当前工作区内容当成归档的反向应用基准。
