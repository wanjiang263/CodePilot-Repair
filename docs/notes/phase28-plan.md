# Phase28 执行计划（T01 契约冻结，2026-10-30）

基线：HEAD `9ccc703`，源码提交 `1e2c443`，tracked 工作树干净，候选 `phase27-candidate-v1`（pending，父 `phase26-candidate-v5`，64 源文件）。
子智能体编制通过 ZCode Agent 工具实现（环境无 spawn_teammate，Agent 工具等价）；主控为 ZCode 主会话。
真实模型请求 0、费用 0；只用回环、临时 SQLite、Fake/Mock。不 push/PR/发布。历史候选与未跟踪文件不修改。

## 契约 A：归档 schema（v2）

- `candidate.json` 顶层 `schema_version=2`，新增 `snapshot_hash_rule="file_bytes_sha256"`、`snapshot_file_sha256`（snapshot.json 字节哈希）、`snapshot_canonical_sha256`（snapshot 解析后 canonical JSON 哈希，`sort_keys` + `ensure_ascii=False`）、`diff_base_commit`、`diff_sha256`（candidate.diff 字节哈希）、`source_hashes`（LF 规范化）+ `raw_worktree_hashes`（工作区原始字节）、`source_files`、`normalization`。
- `code_revision` 字段在 v2 删除；`diff_base_commit` 是 diff 基线的唯一权威字段，不再冒充源码版本。
- `snapshot.json` 保持 `schema_version=1`（兼容 `validate_snapshot`/`load_selection`），policy 内容不变语义。
- 生成器拒绝覆盖已存在版本目录；tracked 脏默认拒绝（显式放行必须记录 `dirty_worktree=true` 与文件清单）；LF + raw 双哈希；diff 用 `git diff --binary <diff_base> -- <paths>`；生成后 `git apply --check -R` 验证；不创建 active 指针；JSON 输出确定性排序。
- 谱系决策：v2 的 `diff_base_commit=1e2c443`（v1 的 source_commit），`source_commit`=本轮源码提交（先生成源码提交，再生成候选），使 candidate.diff 恰为本轮增量。

## 契约 B：HTTP 支持矩阵（三态 verified/unverified/unsupported）

覆盖：openai_compatible+HTTPTransport、native Anthropic+HTTPTransport、Bedrock(boto3，不经 http_lifecycle)、MockTransport、自定义同步 transport、gzip/慢流/延迟头、派发前/派发后取消、取消隔离、无隐藏重试、超时无线程遗留、代理环境/NO_PROXY/trust_env、连接复用。Bedrock 本轮声明 `unverified`（不写"已支持"）。连接复用结论只允许"保留请求私有 client"或"有数据支持的池化方案"。

## 契约 C：索引性能结论

场景分离：冷索引 / 新进程无变化 / 同进程单文件变化 / 新进程单文件变化 / snapshot-only 后变化 / 缓存 128/512/不限。禁止冷热混算、禁止小仓库外推；Django 全量数字只作 Phase27 历史对照，本轮剖析用 300–500 文件子集并标注"不可外推"。每场景 ≥2 预热 + 3 测量取中位数。必须回答：2,589 次解析来源；TEST 闭包/边删除/DB 插入占比；128 缓存是否抖动。

## 任务板

| ID | 任务 | 负责 | 依赖 | 写范围 | 验收 |
|---|---|---|---|---|---|
| T01 | 契约冻结 | 主控 | - | 本文件 | 本文件 |
| T02 | 归档 schema+生成器 | S1 | T01 | `src/codepilot/archive.py`、`scripts/phase28_archive.py`、`tests/unit/test_phase28_archive.py`、`docs/notes/phase28-archive-schema.md` | 生成 v2 成功、字段完整、测试通过 |
| T03 | 索引分阶段剖析 | S3 | T01 | `scripts/phase28_index_profile.py`、`docs/notes/phase28-index-profile.json` | 阶段耗时/解析命中/闭包/内存齐全 |
| T04 | 缓存与搜索收敛分析 | S4 | T01 | `docs/notes/phase28-cache-search.md`（只读） | 有数字结论，不改生产代码 |
| T05 | HTTP 兼容矩阵 | S5 | T01 | `tests/unit/llm/test_phase28_http_matrix.py`、`docs/notes/phase28-http-matrix.md` | 三态矩阵+测试通过 |
| T06 | 归档校验器+旧档案审计 | S2 | T02 | `scripts/phase28_archive_verify.py`、`tests/unit/test_phase28_archive_verify.py`、`docs/notes/phase28-archive-integrity.json` | v2 零未解释失败；旧档案规则推断；历史未改 |
| T07 | 索引修复或旁路 | S3 | T03/T04 | `src/codepilot/repo/index.py`、`src/codepilot/storage/repository.py`、相关测试、`docs/notes/phase28-index-decision.md` | 有数据支撑的修复或明确旁路 |
| T08 | 三层验证 | S6 | T02/T05/T07 | `tests/integration/phase28/`、`.codex-pytest-tmp/phase28/` | L1/L2/L3 有命令/退出码/日志 |
| T09 | 候选 v2+文档 | S7 | T02/T06/T08 | `configs/harness/candidates/phase28-candidate-v2/`、`docs/notes/phase28-runtime-validation.md`、`phase28-runtime-results.json` | pending、parent 正确、哈希一致 |
| T10 | 独立审计 | S8 | T06/T08 | `docs/notes/phase28-independent-review.md` | 抽样哈希/diff/测试/声明边界 |
| T11 | 最终回归+静态检查 | S6 | T07/T09 | 日志 | 失败全处理；静态通过 |
| T12 | 本地提交 | 主控 | T10/T11 | 仅任务文件 | 分拆提交完成，无 push |
| T13 | 清理临时垃圾 | 主控 | T12 | 白名单临时文件 | 清单先行 |
| T14 | 最终汇总 | 主控 | T12 | 无 | 按模板输出 |

执行顺序：`T01 → T02/T03/T04/T05 并行 → T06/T07 → T08 → T09/T10 → T11 → T12 → T13 → T14`。

## 提交拆分

1. `feat: add self-verifying candidate archive`（源码+脚本+测试，含索引剖析决策改动）。
2. 基于该提交生成候选 v2。
3. `docs: record phase28 validation and archive v2`（docs/notes + 候选目录）。
