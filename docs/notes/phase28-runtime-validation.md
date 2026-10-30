# Phase28 真实环境验证与候选交付（2026-10-30，2026-10-31 经两轮 amend 后按最终字节生成）

本轮（S7/T09）在源码提交 `770c0dc` 上生成 schema v2 候选 `phase28-candidate-v2`（parent=`phase27-candidate-v1`，status=pending），并复跑归档完整性校验器（退出码 0）。三层验证的执行记录与逐条命令见 [phase28-validation.md](phase28-validation.md)，本文只做摘要与交付事实归档；机器可读摘要见 [phase28-runtime-results.json](phase28-runtime-results.json)；归档完整性报告见 [phase28-archive-integrity.json](phase28-archive-integrity.json)。全轮约束：零真实模型请求、零外部网络（仅 127.0.0.1 回环）、零费用、禁 Docker、未修改任何历史候选（phase26-*、phase27-*）。

## 候选与哈希

- 基线 HEAD（文档提交）：`9ccc7032ec4feed423b888312630e7b28800b72f`（"docs: deliver phase27 real-runtime evidence and pending candidate"）。
- 本轮源码提交：`770c0dce6bbaa383f86917ec2d74fd085296eedb`（"feat: add self-verifying candidate archive and phase28 diagnostics"，parent 仍为 `9ccc703`，13 文件 +5204/−18），生成候选时即为 git HEAD。它是首轮源码提交 `af0e772` 经两轮 amend（amend 1：T11 静态门禁格式化 → `51aae86`；amend 2：S8 审计 D1 prompts 修复 → `770c0dc`）后的最终字节版本（见"首次失败与修复记录"末两条），候选按最终字节生成。
- diff 基线（唯一权威字段 `diff_base_commit`，即 v1 的 `source_commit`）：`1e2c4430aebae7d7cea45f245a5482658ca7eb1f`。v2 不含 `code_revision` 键，不再用第二个提交字段冒充源码版本。
- 候选版本：`phase28-candidate-v2`；parent=`phase27-candidate-v1`；`status=pending`；`schema_version=2`；`snapshot_hash_rule="file_bytes_sha256"`；`created_from=phase28_archive`（`codepilot.archive.create_candidate`，`allow_dirty=False`，实测 `dirty_worktree=false`）。
- 源文件数：75（v1 的 64 ∪ 本轮增量 11）。双 snapshot 哈希与 diff 哈希（生成 summary 与 candidate.json 一致，校验器独立复算通过）：
  - `snapshot_file_sha256`（snapshot.json 文件字节）：`d0fd3a5ac133637454c87a153aa78ae917204387c5cece0cd8f5c2f8d6de5155`
  - `snapshot_canonical_sha256`（canonical JSON，`sort_keys` + `ensure_ascii=False`）：`def0ec454d6c9258feeef1b940680cb717d14edae3a2811ddd0d4e941d3ca67a`
  - `diff_sha256`（candidate.diff 字节，共 217204 字节）：`f49713553d713fa199230f180f07a27a5f51414a58e2a64e212685db28d45da1`
- **双 snapshot 哈希相对前两轮已改变**（`2e224de6…`/`b182424a…` → `d0fd3a5a…`/`def0ec45…`）——这正是 D1 prompts 修复生效的证据：前两轮的 `prompts:{}` 是生成器缺陷（见"首次失败与修复记录"末条），本轮 snapshot.json 的 prompts 恰含 coder/planner/reviewer 三个非空字符串（361/1823/496 字符），与 v1 snapshot 的 prompts **逐字段相等**。amend 1（纯格式化）不改变 snapshot 哈希；candidate.diff 字节演变：214363（`af0e772`）→ 214987（`51aae86`）→ 217204（`770c0dc`）。
- policy 内容：`instruction=""`（与 v1 一致）；experiences 从 v1 snapshot.json **原样继承**——1 条 L2 经验 `phase26-public-timeout-advice`，`status` 保持 `pending`，内容逐字段未改；prompts 由生成器从 LF 规范化后的 agent 源码重提取（D1 修复后按带前缀清单键的后缀解析角色文件，本轮实测与 v1 逐字段相等）。
- parent_integrity（比较性记录，非裁决）：v1 在 `snapshot_sha256` 字段声明 `6c4f26dbbfd6057f621ec35849f4aaa90ee78927df45c76ecf04bee1ffc2991e`，与其实际文件字节哈希一致（`file_bytes_consistent=true`），与 canonical 不一致（`canonical_consistent=false`，v1 历史声明本就是文件字节口径，与校验器推断 `rule=file_bytes` 一致）。
- 生成流程：先 `--dry-run`（退出码 0，75 文件、dirty_worktree=false，不落盘）确认可生成，再用一次性脚本调用 `create_candidate` 真实生成；候选目录恰 4 文件（snapshot.json / candidate.json / sources.json / candidate.diff）；`git apply --check -R` 反向应用验证通过；`configs/harness/` 下无 `active.json` / `active.next.json`。

## 源文件清单（相对 v1 的 added / changed）

清单口径：v1 `candidate.json.code_snapshot.source_hashes` 全部 64 个路径 ∪ `git diff --name-only 1e2c4430..51aae86` 中属于 `src/`、`scripts/`、`tests/`、`web/` 的路径（该 diff 共 71 条，其中 13 条在范围内，其余 58 条为 docs/notes 与 configs 历史候选文件，不入档）。去重排序后 75 条，全部存在于工作树；三轮成员完全相同。两轮 amend 只改变既有清单内文件的字节：amend 1（格式化）改 `scripts/phase28_index_profile.py`、`tests/integration/phase28/test_phase28_archive_verify_loop.py`、`tests/integration/phase28/test_phase28_index_storage_loop.py`；amend 2（D1 修复）改 `src/codepilot/archive.py` 与 `tests/unit/test_phase28_archive.py`（新增 1 个回归测试，该文件 15→16 项）。

新增 11 条：

- `scripts/phase28_archive.py`
- `scripts/phase28_archive_verify.py`
- `scripts/phase28_index_profile.py`
- `src/codepilot/archive.py`
- `src/codepilot/retrieval/runner.py`
- `tests/integration/phase28/test_phase28_archive_verify_loop.py`
- `tests/integration/phase28/test_phase28_index_storage_loop.py`
- `tests/integration/phase28/test_phase28_registry_loopback.py`
- `tests/unit/llm/test_phase28_http_matrix.py`
- `tests/unit/test_phase28_archive.py`
- `tests/unit/test_phase28_archive_verify.py`

相对 v1 变化 2 条（仍在 v1 的 64 条清单内，本轮被修改）：

- `src/codepilot/repo/index.py`
- `tests/unit/repo/test_index.py`

## 三层验证摘要（数字与日志均引自 [phase28-validation.md](phase28-validation.md)）

- **L1 单元层**：67 + 28 passed（15 归档生成 + 13 归档校验 + 39 HTTP 矩阵；repo/index 组含本轮追加的 2 个缓存参数回归）。日志：`.codex-pytest-tmp/phase28/s6a-l1-phase28-unit.log`、`.codex-pytest-tmp/phase28/s6a-l1-repo-index.log`。
- **L2 本地集成层**：`tests/integration/phase28/` 7 passed，一次通过，无偶发重跑。日志：`.codex-pytest-tmp/phase28/s6a-l2.log`。
- **L3 回归层**：llm 490 passed / repo 381 passed / storage+api 39 passed / observability 53 passed；integration 13 文件合并首跑 99 passed / 4 failed，修复后受影响文件 18 passed（详见下节）。日志：`.codex-pytest-tmp/phase28/s6b-*.log`。L3 各组合计 1066 次执行，与 L1/L2 存在目录重叠，不跨层相加宣称覆盖。

## 首次失败与修复记录

**L3 的 4 个 phase26 遗留失败 → 证伪 → 修复 → 全绿**（第一现场保留于 `.codex-pytest-tmp/phase28/s6b-l3-integration.log`，未覆盖）：4 个失败集中在 `tests/integration/test_retrieval_index_failures.py`，期望失败分类 `code="unreadable"`，实际得到 `"index_execution"`。定位：phase26 提交 `748ad2f` 引入——`Indexer.index()` 在 `scan.unreadable` 非空时抛普通 `OSError`，被 runner 的通用 `except Exception` 包装掉，既有 `unreadable` 分支不可达。证伪"非本轮引起"：`git stash` 本轮改动后在干净 HEAD（`9ccc703`）上单跑仍失败。修复（最小 diff、不改任何测试）：`src/codepilot/repo/index.py` 新增 `IndexScanIncompleteError(OSError)`（携带 unreadable 路径），`src/codepilot/retrieval/runner.py` 在通用捕获前截获并经共享 helper 恢复 `unreadable` 分类。修复后：该文件 18 passed；retrieval+repo 702 passed / 1 skipped（既有 embedding 模型门控）；api+storage 39 passed。日志：`.codex-pytest-tmp/phase28/fix-unreadable-*.log`。

**T07 数据链首败**（S3 线，剖析脚本，日志 `.codex-pytest-tmp/phase28/first-failure-tuple-frame.log`）：`scripts/phase28_index_profile.py --calibrate` 首跑即崩——`PhaseTimer._stack` 用 tuple 存计时帧，`_record` 中 `self._stack[-1][2] += elapsed` 触发 `TypeError: 'tuple' object does not support item assignment`；修复为帧改用 list，随后剖析全流程完成并产出 `docs/notes/phase28-index-profile.json`（T07 决策的数据来源）。

**S2 首败**（归档校验器单测首跑，日志 `.codex-pytest-tmp/phase28/s2-verify-first-failure.log`）：12 收集 / 5 失败（`..F.....FFFFF`）。两类失败：(1) `test_tampered_diff_fails_hash_and_reverse_apply`——首版在 diff 尾部追加垃圾字节做篡改，`git apply` 忽略尾部垃圾导致反向应用仍通过，断言 `failed_checks ⊇ {diff_sha256, diff_reverse_apply}` 不成立（实际只有 `diff_sha256` 失败）；(2) 三个旧档案测试与 `test_archive_outside_git_repo_annotates_skipped_blob_checks` 均崩在测试助手 `_write_fake_old_archive`（`test_phase28_archive_verify.py:408`）——把 `str` 写入二进制路径，`TypeError: memoryview: a bytes-like object is required, not 'str'`（该首跑用了 Anaconda 解释器路径，非 `.venv`）。修复（均可在最终代码中核对）：篡改方式改为改真实 hunk 体（`return 2`→`return 9`，测试内注释说明 git apply 忽略尾部垃圾）；助手统一 `.encode("utf-8")` 后写字节。最终该套件 13 passed（计入 L1-1 的 67）。

**Amend 1（T11 静态门禁格式化，2026-10-31）**：静态门禁发现 3 个本轮文件 ruff format 未过（`scripts/phase28_index_profile.py`、`tests/integration/phase28/test_phase28_archive_verify_loop.py`、`tests/integration/phase28/test_phase28_index_storage_loop.py`）→ 主控格式化并在最终字节上复跑 74 项（archive 15 + verify 13 + http_matrix 39 + integration/phase28 7）全绿 → 源码提交 amend 为 `51aae86`（基于 `9ccc703`、消息不变、13 文件 +5150/−18，提交时间 2026-10-31 00:03 +0800）→ S7 按该字节重生成 `phase28-candidate-v2` 并复跑校验器（退出码 0）。纯格式化不改变 snapshot 双哈希。

**Amend 2（D1：S8 审计发现 prompts 静默丢失，2026-10-31）**：S8 审计发现实质缺陷 D1——生成器 `_extract_prompts` 按裸键 `agents/{role}.py` 查询源文件清单，而实际清单键是 `src/codepilot/agents/{role}.py`，导致前两轮 v2 的 snapshot.json `prompts:{}` 静默丢失三角色提示词（v1 有完整 prompts）。修复：按路径后缀解析角色文件（`PurePosixPath(key).parts[-2:] == ("agents", f"{role}.py")`，多个同名取最短键）；新增回归测试 `test_prompts_resolved_from_prefixed_agent_manifest_keys`（`tests/unit/test_phase28_archive.py` 现 16 项）；静态检查（ruff check/format、mypy）与受影响测试（archive 16 + integration archive_verify_loop 4 = 20 项）全绿后主控 amend 为 `770c0dc`（13 文件 +5204/−18，提交时间 2026-10-31 00:25 +0800）→ S7 删除旧候选目录后第三次重生成：snapshot 双哈希相对前两轮改变（见"候选与哈希"），生成后断言 prompts 恰三角色非空且与 v1 snapshot 逐字段相等，校验器退出码 0。`docs/notes/phase28-archive-schema.md` 的 prompts 段已同步更新为后缀解析描述。

## T11 最终回归复跑与静态门禁（引自 [phase28-validation.md](phase28-validation.md)）

在源码提交上重跑完整相关组（6/6 组全绿，日志 `t11-*.log`）：A1 llm 490 passed / A2 repo 381 passed / A3 storage+api 39 passed / A4 observability 53 passed / A5 integration 110 passed（排除 2 个需 Docker 文件，含 phase28 集成 7 例）。A1–A5 目录互不重叠，去重总数 **1073**；A6 为组合交叉复跑（archive 15 + verify 13 + http_matrix 39 + integration/phase28 7）= **74 passed**，与 A1–A5 部分重叠、不计入去重总数。静态检查：ruff check、mypy（archive.py、index.py、runner.py）、web `tsc --noEmit`、`eslint src --max-warnings=0`、`git diff --check` 全部通过；**ruff format --check 首跑退出码 1**（3 个本轮文件待重排）→ 主控按 format 重排（仅风格）→ 最终字节上复跑 A6 口径 74 项全绿 → amend 进源码提交（即上文 Amend 1）。A6 记录的 74 项运行于 amend 2 之前的字节；amend 2（D1 修复）后的最终字节上复跑的是受影响组合 archive 16 + integration archive_verify_loop 4 = 20 项（全绿，见 Amend 2 记录）。

## 索引剖析与旁路决策（引自 [phase28-index-profile.json](phase28-index-profile.json) / [phase28-index-decision.md](phase28-index-decision.md)）

合成子集（450 个约 1KB Python 文件，seed 20281030，绝对耗时不可外推 Django）上，单文件增量（sameproc_change 中位 wall=2.671s）的成本主体是关联闭包而非解析：闭包复合体（边提取 45.9% + 边删除/写回 12.6% + 符号替换 3.6% + 上下文构建 5.5%）≈ 67%，解析仅 1.7%（322 次解析 = 450 − 缓存命中 128；Django 量级同构：2589 = 2717 − 128）。缓存 128 / 512 / 不限对照显示扩到 512 只省解析 0.042s、wall 0.29s（≈11%），触碰不到闭包且在大仓形态下 8MiB 源字节上限先于条数绑定——**默认 128 条 / 8MiB 保持不变**，本轮新增的 `cache_max_entries` / `cache_max_bytes` 仅为 opt-in 实验通道，4 个生产调用点全部用默认值。决策为**选项 4：旁路 + 文档化**——无变化走 `verified_snapshot_v2` 指纹快路径（不解析、不写图），有变化走 `complete_closure_v2` 完整闭包（与同状态全新全量索引逐字段等价）；一切"局部重建边/TEST 边"的方案因未解析绑定不可恢复而被正确性否决。本决策不承诺任何提速百分比。

## HTTP 兼容矩阵摘要（引自 [phase28-http-matrix.md](phase28-http-matrix.md)）

39 个测试、openai_compatible 与 native anthropic_messages 双 adapter 参数化、3 连跑全过，全部流量走 `127.0.0.1` 回环临时端口、假凭据、零真实请求。三态结论：**verified** 22 行（完整响应/usage 解析、总 deadline 各形态截断、派发前/后取消、取消隔离、无隐藏重试恰好 1 次线上请求、无线程遗留、gzip 恰好解码一次、MockTransport 注入路径、自定义同步 transport 被拒、代理环境/NO_PROXY/trust_env、连接复用观测，另含 Bedrock 执行路径旁路 `http_lifecycle` 的结构验证）；**unverified** 5 项（Bedrock 的 deadline/派发后取消/usage 冻结、DNS 与 OS 级停顿、池化复用下的取消语义、GLM 之外的真实服务商、HTTPS/TLS 回环）；**unsupported** 3 项（自定义同步阻塞 transport、注入 client 的 TLS/代理等设置不复制到实际请求、非 HTTPTransport 无取消感知）。**连接复用结论 = 保留请求私有 client**：row14 回环观测 5/5 条独立 TCP 连接（对端端口互不相同），池化与 Phase27 冻结的"请求级取消后关闭传输再返回"语义冲突，且回环实测单次请求中位 ~15ms，无数据支持改池化。

## 未运行 / 未验证项（明确边界，不夸大）

- **真实服务商**：本轮 0 请求、0 费用。GLM 真实链路仅有 Phase27 记录（`docs/notes/glm-live-hardening.md` 与 Phase27 验收记录，此处引用不重复），本轮未复跑；OpenAI / Anthropic / Bedrock 真实端点行为未验证。
- **Docker / 官方 Harness**：本轮未运行（禁 Docker；Phase27 的 Docker 与官方 Harness 记录属历史证据，不因本轮复用而刷新）。
- **Bedrock deadline / 派发后取消 / usage 冻结**：unverified——执行路径已证明绕过 `http_lifecycle`，Phase27 冻结的取消与总 deadline 保证不适用于该路径，真实 AWS SDK 行为未联网验证。
- **Web E2E**：本轮 diff 无 `web/` 变更（13 条范围内路径全部为 src/scripts/tests），前端零改动，未重复运行前端测试/E2E。
- **Django 全量剖析**：本轮未复跑，只引用 Phase27 冻结历史数字（2717 文件、冷索引 105.0s、单文件增量 142.7s、解析 2589 = 2717 − 128）；本轮子集占比结构与之方向一致但数值不可直接搬用。
- **声明口径**：L3 修复后"全绿"仅指本轮运行的组合与排除清单（Docker/swebench 排除、1 skipped 为既有 embedding 门控）；本地回环验证 ≠ 真实服务商行为；本文不写解决率、不写提速百分比、不写任何未经本轮运行支持的性能数字。

## 约束合规

- 真实模型请求数 0；新增费用 0 CNY。
- 未 push、无 PR、无发布；S7 零新 git commit（源码提交 `770c0dc` 是主控沿 `af0e772` → `51aae86` → `770c0dc` 两轮 amend 链形成的既有提交，非 S7 所建）；候选与文档交由主控按 T12 计划分拆本地提交，**最终 docs 提交 ID 由聊天交付记录提供，避免文档自引用循环**（Phase27 同款处理）。
- 无 active 指针：`configs/harness/` 下无 `active.json` / `active.next.json`，校验器 `not_active` 检查通过。
- 历史候选未修改：`git status --porcelain -- configs/harness/candidates/phase27-candidate-v1` 为空；校验器 `historical_files_modified=false`（git status 无 tracked 变更 + 旧档案 blob 基线比对无违例）。
