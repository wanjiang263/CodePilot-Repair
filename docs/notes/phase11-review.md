# Phase 11 跨模块 Review：smoke（E-17）/ runner（E-19）/ prediction（E-15）

> Reviewer：Owner D（harness）  
> Review 时点：**2026-10-19 02:14:53（Asia/Shanghai）**  
> 方式：只读代码审查 + `%TEMP` 下一次性探针脚本 + 运行既有测试；**未修改**任何被 review 文件。  
> 本文件是本次 review 唯一新增的文件。
>
> **文件仍在变化（重要）**：review 期间 `swebench_runner.py` 被 Owner F 连续修改
> （02:09:43），`swebench_smoke.py` 02:02:36 也在改。下面所有行号以 review 时点快照为准；
> 已在过程中观察到修复的项会单独标注，落单前请以最新文件复核一遍。

| 文件 | mtime（review 时快照） |
|---|---|
| `scripts/swebench_smoke.py` | 2026-10-19 02:02:36 |
| `src/codepilot/eval/swebench_runner.py` | 2026-10-19 02:09:43 |
| `src/codepilot/eval/swebench_prediction.py` | 2026-10-19 01:55:39 |
| `src/codepilot/eval/swebench_harness.py`（D，仅作对照） | 2026-10-19 01:59:24 |

## 0. 结论摘要

| 级别 | 数量 | 一句话 |
|---|---|---|
| **P0** | 1 | D 与 F/smoke 的官方 harness `run_id` 摘要算法不一致：任何走 `SWEBenchRunner` 的评测（`--resume` 路径）都会在**执行官方 harness 之前**抛 `HarnessCacheError`。E-17 主链路不受影响，E-19 评测链路在 Linux/WSL 上 100% 变成 ERROR。 |
| **P1** | 4 | ① smoke evaluator 未把评测阶段 error_kind 映射成 F 的 `ERROR` verdict（P0 修好后才会暴露，会让 Windows resume 的 `evaluation_status` 从 ERROR 变 NOT_RUN、现有测试失败）；② 环境/镜像身份（Owner C 身份、D 的 `image_ids`、F 的 `environment_identity`）没有从 smoke 贯通，两处缓存都退化为"未验证镜像"；③ smoke 失败归因粒度不足（BUDGET/ENV/HARNESS 被压成一类，Lead 要求的四类不是一等字段）；④ smoke 默认产物路径不是验收要求的 `artifacts/swebench/<run_id>/`。 |
| **P2** | 10 | 见 §3；主要是 `real_docker` 未进信封、哈希 helper 不一致、rename/mode-only/content-line 三个 patch 边缘、测试缺口等。 |

**通过项（无阻塞）**：smoke dry-run 确实不调 LLM/不碰 Docker（代码+既有测试）；三字段 predictions 与空补丁默认不落官方行；官方结论优先、内部 M2 不被当成 official resolved；F 的评测级缓存身份与旧报告拒绝逻辑是完整且 fail-closed 的；prediction 的绝对路径/`..`/`.git/`/`eval/`/盘符/反斜杠拦截有效。

---

## 1. P0（必须在 Linux/WSL 上的真实 E-17/E-19 验收前修复）

### P0-1 `harness_run_id` 摘要算法 D/F 不一致，评测链路必然失败

**涉及位置**

- Owner F：`src/codepilot/eval/swebench_runner.py:524-534`（`harness_run_id` 返回
  `cp-{patch_sha256[:12]}-{seed}`）；`:2293`（`_run_evaluation` 用该值作为
  `expected_run_id`）；`:2296-2304` 把它传给 evaluator。
- Owner E（smoke）：`scripts/swebench_smoke.py:1208`（`HarnessConfig(run_id=harness_run_id)`）、
  `:1235`（回填到 `SWEBenchEvaluation.harness_run_id`）。
- Owner D：`src/codepilot/eval/swebench_harness.py:1379`（`run()` 调用
  `_enforce_run_id_digest`）、`:1827-1847`（`cp-<12hex>-<seed>` 的摘要必须等于
  `patch_set_digest(patch_hashes)[:12]`，即 `sha256([["instance_id","patch_sha256"]])[:12]`）。

**复现依据（读代码 + 一次性探针，无需真实 harness）**

探针（`%TEMP/cp_review_runid_probe.py`，未提交）构造同一份 patch，分别用 F 的方案与
D 的方案生成 run_id，然后用真实 `OfficialHarnessRunner.run()` 跑（platform 指定 Windows，
但校验发生在平台判断之前）：

```
F harness_run_id = cp-7180bafcf75e-20261019
D derive_run_id  = cp-de525fea3255-20261019
REPRO: HarnessCacheError kind= CACHE_IDENTITY_MISMATCH
detail: run_id 摘要 '7180bafcf75e' 与当前补丁集合摘要 'de525fea3255' 不一致；
        不同补丁必须使用 derive_run_id() 生成新 run_id
```

`HarnessError` 继承自 `RuntimeError`；F 在 `:2306-2318` 已把 `HarnessCacheError`
映射为 `verdict=ERROR, error_kind="harness_cache_error"`，所以：

- **Windows 上**：`tests/integration/test_swebench_smoke.py:874-906` 的 resume 测试
  仍然绿，因为它只断言 `evaluation_status == "ERROR"`，而真实原因从
  "平台不可执行 ENV_ERROR" 变成了 "缓存身份不符 harness_cache_error"（测试通过，
  归因被掩盖）。
- **Linux/WSL 上**：同样的异常在官方 harness 启动前抛出 → 每条评测都 ERROR →
  E-19 的评测级复用/真实评测永远测不到；E-17 `--resume` 用例也无法给出
  OFFICIAL_RESOLVED/UNRESOLVED。
- E-17 **非 resume 主链路**不受影响：`SmokePaths.build` 的 run_id 形如
  `swebench-smoke-...`，不匹配 `cp-<12hex>-<seed>` 正则，D 的负控制跳过。

**修复建议（二选一，需 Lead 定契约措辞）**

- 方案 A（最小改动，推荐）：D 放宽 `_enforce_run_id_digest`——当
  `patch_hashes` 恰好只有一个实例时，同时接受该 patch 的 `sha256[:12]`
  （即 F 的 `cp-{patch_sha[:12]}-{seed}`）作为合法摘要。缓存 sidecar 仍会绑定
  `instance_id + patch_hash + dataset_revision + harness_version`，负控制不削弱。
  同时把契约 §7 的 `patch_set_sha256` 澄清为"单实例时可用补丁自身 sha256"。
- 方案 B（单一真源）：F 改为导入 D 的 `derive_run_id({instance_id: patch_sha256}, seed=...)`，
  `harness_run_id` 增加 `instance_id` 参数；需同步改
  `tests/eval/test_swebench_runner.py:572-576` 的断言。
- 无论哪种，修复后请把 Windows resume 测试增强为断言
  `records[0]["error_class"] == "env_error"`（或 `evaluation_status` 的新语义），
  否则 P0 仍可能被"恒定 ERROR"掩盖。

---

## 2. P1（本轮应修，至少写入已知限制）

### P1-1 smoke evaluator 未把"评测阶段失败"映射成 F 的 `ERROR` verdict（P0 修好后暴露）

**位置**

- Owner E：`scripts/swebench_smoke.py:1225-1246`——`verdict = execution.verdict_for(...)`
  后只把 RESOLVED/UNRESOLVED 映射成 bool，`error_kind` 原样透传，但 **verdict 仍可能是
  `NOT_EVALUATED`/`MISSING`**；`:1235` 把它回填给 F。
- Owner F：`src/codepilot/eval/swebench_runner.py:2689-2696`（`evaluation_state` 只有在
  `official_verdict == "ERROR"` 时才记 `ERROR`，否则记 `NOT_RUN`），
  `:1151-1160`（`OfficialEvidenceAudit.reusable` 要求可判定结论）。
- Owner E 的测试：`tests/integration/test_swebench_smoke.py:874-906` 明确期望
  Windows 平台限制记 `evaluation_status == "ERROR"`。

**现象/依据**：P0 修复后，D 在原生 Windows 返回
`decision=PLATFORM_BLOCKED, error_kind=ENV_ERROR`，逐实例 verdict 是
`NOT_EVALUATED`（harness.py 的 `_fallback_verdicts`；`run()` 的
`HarnessRunDecision.PLATFORM_BLOCKED` 分支）。smoke evaluator 会把
`NOT_EVALUATED` 当作 verdict 交给 F → F 记 `evaluation_status=NOT_RUN`
（但 attribution 仍是 ENV_ERROR），与 smoke 自己测试的预期 ERROR 冲突。
Docker 不可用/TIMEOUT/REPORT_MISSING 等 `MISSING` 情形同理。

**建议**：在 smoke evaluator 中，若 `execution.error_kind is not None`
（或 `verdict in {MISSING, NOT_EVALUATED}` 且存在 error_kind），就把
`runner_module.OfficialVerdict.ERROR` 作为 verdict、`resolved=None`，并保留
`error_kind` 字符串。这样 F 的 `evaluation_state=ERROR` 与 smoke 测试语义一致；
或者 F 在 `_build_record` 里把"有 error_kind 的评测"也映射成
`EvaluationState.ERROR`（二选一，不要两边各改一半）。

### P1-2 环境/镜像身份未从 Owner C 贯通到两处缓存（复用强度不足）

**现状（hook 已就绪，调用方没接）**

- Owner F 已加：`SWEBenchRunConfig.environment_identity`（`swebench_runner.py:543-552`）、
  写入评测信封（`:2470`）、评测级恢复审计（`:1308-1322`，注入了就必须一致）。
- Owner D 已加：`OfficialHarnessRunner(..., image_ids=...)`，写进缓存身份
  (`HarnessCacheIdentity.image_ids`)，不同镜像 id 拒绝复用（`tests/eval/test_swebench_harness.py`
  的 `test_cache_reuse_requires_same_image_identity`）。
- **缺口**：
  - `scripts/swebench_smoke.py:1294-1306` 构造 `SWEBenchRunConfig` 时没有传
    `environment_identity`，尽管 `_run_resume` 的入参 `environment`（`:1342`）就是
    Owner C 的 identity dump（含 `instance_image_key/env_image_key/base_image_key/
    environment_hash`）。
  - `scripts/swebench_smoke.py:1206-1222` 构造 `HarnessConfig` 时没有传
    `image_ids`，D 的官方报告缓存身份里 `image_ids=()`。
- 结果：E-19 评测级恢复与 D 的官方报告缓存都只能做 `None==None` 的弱校验；
  同一 patch/revision/harness 版本、不同镜像重建出来的旧报告理论上可被复用。

**建议**：在 `_run_resume` 里从 `environment` 汇总一个稳定字符串
（例如 `sha256(canonical({instance_image_key, env_image_key, base_image_key,
environment_hash}))`）传给 `SWEBenchRunConfig.environment_identity`；在
`_make_evaluator` 里传 `image_ids={instance_id: instance_image_key + "|" +
env_image_key + "|" + base_image_key}`。Lead 已同意该注入来源为 Owner C 的
`SWEBenchEnvironmentIdentity`。

### P1-3 smoke 失败归因粒度不足（与 Lead 的四类要求不一致）

**位置**：`scripts/swebench_smoke.py`

- `:1132-1147` `_overall_status`：非 `PATCH_READY` 一律
  `SMOKE_COMPLETED_NO_PATCH`；`evaluation_status=ERROR` 时（`:1138-1141`）
  一律 `HARNESS_ERROR`。于是 `prediction.status` 里的
  `BUDGET_EXHAUSTED` / `ENV_ERROR` / `INVALID_PATCH` / `WORKFLOW_STOPPED`
  在**一级 status** 上不可区分（只留在嵌套的 `prediction.status` 里）。
- `:1071-1080`：harness 调用抛异常时只写 `{"error_detail": str(exc), "verdicts": []}`，
  **没有 error_kind / 归因**；返回错误结果的分支也只把任何 error_kind 折成
  `HARNESS_ERROR`。
- 对照：`_classify_prediction`（`:523-548`）其实算出了
  `PredictionStatus.ENV_ERROR/BUDGET_EXHAUSTED`；`_m2_vs_official`（`:711-733`）
  也区分了官方 RESOLVED/UNRESOLVED/NOT_EVALUATED。信息在，但没有提升为报告口径。

**建议**：报告新增一级字段 `attribution`（复用 F 的 `SWEBenchAttribution` 或本地枚举），
至少覆盖 `ENV_ERROR / HARNESS_INTERNAL_ERROR / OFFICIAL_NOT_RESOLVED /
OFFICIAL_RESOLVED / BUDGET_STOPPED / PATCH_INVALID / NOT_EVALUATED`；异常路径用
`codepilot.errors.classify_exception` 填 `error_class/error_kind`（F 在
`swebench_runner.py:2306-2333` 已有可抄的做法）。保留原 `${prediction.status}`、
`${harness.error_kind}` 便于追溯。

### P1-4 默认产物路径与验收要求 `artifacts/swebench/` 不一致

**位置**

- `scripts/swebench_smoke.py:56`：`DEFAULT_ARTIFACT_ROOT =
  ROOT / "artifacts" / "phase11" / "swebench-smoke"`。
- `SmokePaths.build`（`:166-196`）：run 现场在 `<root>/runs/<run_id>`、
  predictions 在 `<root>/predictions/<run_id>.jsonl`、官方评测在
  `<root>/eval/<run_id>`、报告在 `<root>/reports/<run_id>.json`。
- `_default_harness_runner`（`:409-435`）构造 `OfficialHarnessRunner` 时**没有传
  `artifacts=ArtifactStore(...)`**，D 的证据落在 `<root>/eval/<run_id>/_harness/`。

**依据**：任务书 E-16/E-17 验收写的是"评测日志保存到 `artifacts/swebench/`"，
本次要求是 `artifacts/swebench/<run_id>/`。默认命令
`python scripts/swebench_smoke.py --instance-id ...` 产出不满足该路径口径
（虽然全部产物仍在 `<artifact_root>` 下，可追溯）。

**建议**：把默认 root 改成 `ROOT / "artifacts" / "swebench"`，或在 README/验收命令中
强制 `--artifact-root artifacts/swebench`；并把 `ArtifactStore` 传给 D 的 runner
（D 会自动写 `artifacts/runs/<run_id>/swebench_harness/` 证据）。若不改默认值，
至少在报告里输出"验收口径路径"字段，避免 E-17 验收时误判缺失。

---

## 3. P2（记录/后续，不阻塞本轮）

### P2-1 `real_docker` 未写入评测信封，恢复后恒为 False；smoke 适配层硬编码 False

- F：`_write_official_envelope`（`swebench_runner.py:2441-2473`）没有 `real_docker`
  字段，`_recover_evaluation`（`:2626`）用 `envelope.get("real_docker", False)` 读，
  所以评测级复用的 `SWEBenchEvaluation.real_docker` 永远 False。
- smoke：`scripts/swebench_smoke.py:1244` 硬编码 `real_docker=False`——在 Linux/WSL
  真实跑通官方 harness 后，报告仍然声称"非真实 Docker"。
- 建议：信封加 `real_docker`（取自 `SWEBenchEvaluation.real_docker`）；smoke 从
  `execution.decision/decision != PLATFORM_BLOCKED 且 exit_code==0` 或 Owner C 的
  env `ensured` 推导，而不是常量。

### P2-2 哈希 helper 不一致（lone surrogate 边界）

- F：`swebench_runner.py:155-156` 的 `_sha256_text` 用
  `text.encode("utf-8", "surrogatepass")`；D（`swebench_harness.py` 的 `_sha256_text`）
  与 Owner A（`swebench_prediction.patch_sha256`）用普通 `utf-8`。
- 正常文本两者一致；若模型输出含 lone surrogate（极端），同一 patch 在 F 与 D/A
  会得到不同 sha256 → 身份误判/INVALID。建议统一为一个 helper（或统一用
  `surrogatepass` 并在两处文档说明）。

### P2-3 F 的 `harness_run_id` 不含 `instance_id`

- `swebench_runner.py:524-534` 只对 `patch_sha256` 哈希。当前 F 每次评一个实例且
  报告目录是 per-task `run_dir`，安全；但契约 §7 允许一个 `HarnessConfig.instance_ids`
  批量评测（一个 run 一个汇总报告文件名），若未来批量复用该方案，**不同实例的相同
  patch 会共享 run_id/report 文件名**，有冲突风险。建议批量调用方改用 D 的
  `derive_run_id`（其摘要包含 instance_id）。

### P2-4 smoke `run_smoke_set` 的 `comparison_path` 只写进内存返回字典

- `scripts/swebench_smoke.py:1389-1391`：comparison 文件写盘后，只给
  `reports` 的元素 `report["comparison_path"] = ...`；而这些 report 文件在
  `run_smoke()` 内已经 `_atomic_write_json` 过，**磁盘上的 report 没有
  `comparison_path`**。重读报告/验收脚本从磁盘读不到该字段。建议写 comparison 后
  再回写两份 report，或把 comparison 路径写进 comparison 自身。

### P2-5 smoke dry-run 会创建 artifact 目录（不违反安全边界，记录）

- `_prepare_paths`（`:749-767`）在 dry-run 时也会 `mkdir` artifact_root、
  predictions/eval/usage/cost/report/journal 的父目录，并写最终 report。
  不调 LLM、不建镜像、不建 workspace（既有测试
  `tests/integration/test_swebench_smoke.py:534-557` 已断言），因此不算违规；
  若要求"dry-run 零副作用"，需把 mkdir 改成仅检查存在性。

### P2-6 prediction 明确拒绝 rename diff（已知限制，需文档化）

- `swebench_prediction.py:434-461`（`_parse_diff_git_header`，`a/` 与 `b/` 路径不同
  直接 `PatchValidationError`）、`:541-542`（`rename from/to`/`similarity index` 行直接
  拒绝）、`:557-560`（无 `diff --git` 头时同样拒绝）。
- 契约 §4 只要求"多文件/新建/删除"，rename 不是硬性要求；但模型若输出合法
  rename 补丁（或 `similarity index` 行），会被记成 `INVALID_PATCH`、不送官方评测，
  影响 resolved rate 的诚实性。建议至少写入 E-15 的"已知限制"，并在 Phase 12
  重新评估是否支持（strip rename 元数据后按 delete+add 处理，或直接透传给
  `git apply` 并依赖 apply 失败归因）。

### P2-7 mode-only patch（`old mode`/`new mode`，无 hunk）被拒

- `swebench_prediction.py:575-578`：`has_body=False` 且只有 mode 行时抛
  "diff 段落没有 hunk"。纯权限位变更（`chmod +x`）是合法补丁，当前会 INVALID。
  SWE-bench Lite 里概率低；记录为限制。

### P2-8 hunk 内容行以 `--- a/` / `+++ b/` 开头会被误判为多文件标记

- `swebench_prediction.py:550-551`：在 hunk 内遇到 `line.startswith(("--- a/",
  "+++ b/", ...))` 直接抛"多文件补丁必须带 diff --git 头"。
- 探针复现：删除一行内容为 `-- a/comment` 的 SQL/Lua 注释时，diff 行是
  `--- a/comment`，被误判。概率低但属误报（合法补丁 → INVALID_PATCH）。
  建议只在该行处于 `@@` 之前/或前一行是完整 section 结尾时才当文件标记，或用
  hunk 行数计数卸载（`@@ -N,M +N,M @@` 里的行数）来判断 hunk 边界。

### P2-9 prediction 测试缺口（no-newline / rename / multi-hunk）

- `tests/eval/test_swebench_prediction.py:354-434` 覆盖了 CRLF、多文件、新建/删除、
  绝对路径/`..`/`.git`/盘符/反斜杠、headerless diff、repro 目录剔除；**没有**显式用例
  覆盖 `\ No newline at end of file`、rename、同一文件多处 hunk。
- 本次探针已确认当前实现：`\ No newline at end of file` 原样保留（含多文件时）、
  多处 hunk 正常保留、rename 被拒（见 P2-6）。建议补三条回归测试，防止后续
  "顺手支持 rename" 时把 no-newline 弄坏。

### P2-10 两条 predictions 写出路径可能漂移

- 主链路用 Owner A 的 `PredictionWriter`（smoke `:550-592`，带 normalize/安全校验/
  manifest/原子写）；resume evaluator 在 `:1195-1204` **直接**写
  `predictions.jsonl`（三字段正确，但绕过 Owner A 的 normalize/路径安全校验，
  依赖 F 传入的 patch 已在 assembly 校验过）。建议 evaluator 复用
  `PredictionWriter.load/normalize_patch` 或至少调用 `normalize_patch` 后再写，
  避免两套写出口径漂移。

---

## 4. 逐项 checklist 结论

### 4.1 `scripts/swebench_smoke.py`（Owner E，E-17）

| Lead 要求 | 结论 | 依据 |
|---|---|---|
| `--dry-run` 不发起任何 LLM 请求 | **PASS** | `_require_authored`（`:292-302`）dry-run 直接返回；`run_smoke` 在 dry-run 分支（`:1013-1039`）早于 `_run_inference`（`:604-631`）；`_default_provider_bundle`/`_load_llm_config`（`:348-372`）不在该分支。测试 `test_dry_run_uses_real_loader...` 绿。 |
| `--dry-run` 不构建/拉取镜像 | **PASS** | `_default_environment_adapter`（`:318-327`）dry-run 时 `docker=None`；`_ensure_environment`（`:634-655`）dry-run 只 `adapter.identity(task)`，不 `preflight/ensure`；无 `DockerSandbox/WorkspaceManager` 构造。 |
| harness 正确传 `dataset_snapshot`（本地 jsonl） | **PASS**（主链路+evaluator） | `_default_harness_runner` `:414-434`（`Path(loader.snapshot_dir)/f"{split}.jsonl"`）；evaluator `:1217-1221`；D 用它作为 `-d` 并校验 manifest revision。 |
| 传 `report_dir/run_id/model_name_or_path/instance_ids` | **PARTIAL** | `report_dir/model/instance_ids` 正确（`:1209-1221`）；`run_id` 来自 F 的 `cp-{patch[:12]}-{seed}`，与 D 的摘要口径不一致 → **P0-1**。 |
| 消费 `HarnessExecutionResult`，不把内部 m2 当 official resolved | **PASS** | `_harness_payload`（`:662-696`）只取 `execution.verdicts/report/error_kind/...`；`_official_verdict`（`:698-709`）以官方 verdict 为准；`_m2_vs_official`（`:711-733`）`official_authoritative=True`，m2 只进 discrepancy。 |
| predictions.jsonl 严格三字段 | **PASS** | 主链路 `PredictionWriter`（A 模块 `_encode_records` 只写三字段；`_decode_records` 拒绝多字段/空行）；evaluator 直接写入的记录也只有三字段（`:1196-1204`）。 |
| 空补丁默认不写官方行 | **PASS** | `PredictionWriter` 默认 `include_empty=False`（A `:695-711`、`:752-802`）；smoke 未传 include_empty；测试 `test_empty_patch...`（integration `:712-727`）断言文件为空、manifest 记录 EMPTY_PATCH。 |
| Artifact 落 `artifacts/swebench/<run_id>/` | **FAIL（P1-4）** | 默认 root 为 `artifacts/phase11/swebench-smoke`（`:56`），布局为 `runs/eval/predictions/reports`（`:166-196`）；未向 D 传 `ArtifactStore`。 |
| 失败归因区分 ENV_ERROR / HARNESS_INTERNAL_ERROR / OFFICIAL_NOT_RESOLVED / BUDGET_STOPPED | **PARTIAL（P1-3）** | 环境就绪失败有 `status=ENV_ERROR`（`:1000-1012`）；其余在 `_overall_status`（`:1132-1147`）被压成 `SMOKE_COMPLETED_NO_PATCH`/`HARNESS_ERROR`；异常路径（`:1071-1080`）丢 error_kind。 |

### 4.2 `src/codepilot/eval/swebench_runner.py`（Owner F，E-19）

| Lead 要求 | 结论 | 依据 |
|---|---|---|
| `SWEBenchEvaluation` 与 D 的 `HarnessExecutionResult`/三态 resolved 映射一致；apply-failure 不得变 False/True | **PASS（代码层面）** | `SWEBenchEvaluation.__post_init__`（`:722-743`）强制"RESOLVED⇒True / UNRESOLVED⇒False / 其它⇒None"；smoke evaluator 只把 RESOLVED/UNRESOLVED 映射 bool（`:1227-1230`），PATCH_APPLY_FAILED/EMPTY_PATCH 走 None。D 侧 `OfficialVerdict.resolved_value` 已同步为"仅 RESOLVED/UNRESOLVED 给 bool"。**注意 P1-1**：评测阶段错误需要映射成 F 的 `ERROR` verdict，否则 `evaluation_state` 变 NOT_RUN。 |
| 评测级缓存：patch hash 变化 → run_id 变化 | **PASS** | `harness_run_id`（`:524-534`）：`cp-{patch_sha[:12]}-{seed}`，不同 patch/seed 必不同（F 测试 `test_different_patch_gets_a_different_harness_run_id`）。但 F/smoke 与 D 的算法不一致 → **P0-1**。 |
| 旧报告是否可能被误复用 | **PASS（F 侧）** | `audit_official_evidence`（`:1212-1322`）逐项核对 instance/patch/harness_run_id/harness_version/dataset_revision/seed/config_hash/report sha，`OfficialEvidenceAudit.reusable`（`:1151-1160`）要求"无 problem + 可判定 + 报告存在"；`_recover_evaluation`（`:2585-2629`）任一不符返回 None 交给重评；`_record_evidence_intact`（`:2480` 起）连补丁证据哈希一起核。旧报告不能冒充新 patch。 |
| `image_ids` 从环境身份注入 | **PARTIAL（P1-2）** | F 已加 `SWEBenchRunConfig.environment_identity`（`:543-552`）+ 信封（`:2470`）+ 审计（`:1308-1322`），但 smoke 及当前调用方未注入；D 的 `image_ids` 也未传。两处缓存退化为"空==空"。**另**：F 原有 `_OFFICIAL_ERROR_KIND_MAP` 已在 02:09 后补上 `docker_unavailable` 等映射（`:307-342`），D 的其余 `HarnessErrorKind` 也已归到 `OFFICIAL_EVALUATION_ERROR`，本项不再作为缺口。 |

### 4.3 `src/codepilot/eval/swebench_prediction.py`（Owner A，E-15）

| Lead 要求 | 结论 | 依据 |
|---|---|---|
| `\ No newline at end of file` 不被弄坏 | **PASS**（探针验证） | hunk 内行前缀允许集合包含 `\`（`:547-553`），`normalize_patch` 只做 CRLF→LF / 去前后杂物 / 保一个结尾换行（`:610-636`），标记行原样保留。探针：单文件尾、多文件第一个文件尾均保留。建议补测试（P2-9）。 |
| 多处 hunk 不被弄坏 | **PASS**（探针验证） | `_parse_section` 在 hunk 内允许后续 `@@`（`:505-512`），`normalize_patch` 逐行 join 不重排。 |
| rename 不被弄坏 | **KNOWN LIMITATION（P2-6）** | 明确拒绝：`a/`≠`b/`（`:455-460`）、`rename from/to`、`similarity index` 行（`:541-542`）、无 header 的 rename（`:557-560`）。探针复现拒绝。契约不要求 rename，但合法 rename 补丁会 INVALID_PATCH。 |
| 绝对路径 / `..` / `.git/` 不放行 | **PASS** | `_validate_repo_path`（`:407-427`）：拒绝 `/` 开头、盘符、`//`、反斜杠、`.`/`..` 段、`.git`/`eval` 段；`_DENIED_PATH_PARTS={".git","eval"}`（`:63`）。测试 `test_unsafe_or_absolute_paths_are_rejected`（`:379-398`）+ 本次探针（`//etc/passwd`、`../secret`、`.git/config`、`C:/tmp/x`、`src\calc.py`）均拒绝。 |
| 空补丁默认不写官方行 | **PASS** | `PredictionWriter.write`（`:752-802`）只在 PATCH_READY 或显式 `include_empty=True` 时写 EMPTY_PATCH 行；manifest 记录 `status_counts`/`written=False`。 |

---

## 5. 复核命令与输出（只读）

```
# 既有测试（确认当前代码状态；注意 resume 测试绿不代表归因正确，见 P0/P1-1）
.venv\Scripts\python.exe -m pytest tests/integration/test_swebench_smoke.py -q
  → 14 passed in 61.61s
.venv\Scripts\python.exe -m pytest tests/integration/test_swebench_smoke.py -k resume -q
  → 2 passed in 22.39s
.venv\Scripts\python.exe -m pytest tests/eval/test_swebench_prediction.py -q
  → 43 passed in 20.06s
.venv\Scripts\python.exe -m pytest tests/eval/test_swebench_runner.py -q -x
  → 61 passed in 212.71s

# 一次性探针（%TEMP，未提交、未修改仓库文件）
%TEMP%\cp_review_runid_probe.py       → P0-1 复现（F/D run_id 摘要不同 → HarnessCacheError）
%TEMP%\cp_review_prediction.py        → no-newline 保留 / multi-hunk 保留 /
                                        rename、absolute、..、.git 拒绝 / hunk 内容行误判复现
%TEMP%\cp_review_prediction2.py       → 多文件 no-newline 保留 / mode-only 拒绝 /
                                        rename same-path 拒绝
```

## 6. 建议处理顺序

1. **P0-1**（D+F 协调，Lead 定契约措辞）——不修则 E-19 评测链路在 Linux/WSL 上不可用。
2. **P1-1**（smoke evaluator 映射）——与 P0 同一批改，并增强 Windows resume 测试断言。
3. **P1-2**（环境身份注入）——smoke 传 `environment_identity` + `image_ids`，两处缓存升级为强校验。
4. **P1-3/P1-4**（smoke 报告归因与产物路径）——E-17 验收/证据可读性。
5. P2 项按 Owner 排期；P2-6/7/8/9（prediction 边界）建议至少写入 E-15 已知限制并补测试。

> 再次提醒：`swebench_runner.py` / `swebench_smoke.py` 在 review 期间仍在变化，
> 修复前请先 `git diff`/重读最新文件确认行号与逻辑。

---

## 修复复核（最终）

> 复核时点：**2026-10-19 02:28:56（快照）/ 测试至 02:36**。  
> 方式：只读代码审查 + `%TEMP` 探针 + 既有测试；**未修改任何代码/被 review 文件**，
> 本次只在报告末尾追加本小节。  
> 快照 mtime：`swebench_runner.py` 02:22:49、`swebench_smoke.py` 02:21:50、
> `swebench_prediction.py` 02:18:02、`swebench_harness.py`（D）01:59:24 未变。

**最终结论：P0 1/1 = FIXED；P1 4/4 = FIXED；F 的 P2-1/P2-2 = FIXED。**
没有 STILL_OPEN 的 P0/P1。唯一保留的是 prediction 的 rename 限制（P2-6，**设计选择 /
已知限制**，非缺陷，建议写入 E-15 文档）。

| 编号 | 状态 | 复核证据（文件:行号 / 探针） |
|---|---|---|
| P0-1 run_id 算法不一致 | **FIXED** | F `harness_run_id(instance_id, patch_sha256, seed)` 改为直接 `derive_run_id({instance_id: patch_sha256}, seed=str(seed))`（`swebench_runner.py:528-547`；import 于 `:93-100`）；所有调用点都带 `base.instance_id`（`:2322`、`:2631`、`:2646`、`:2772`）。smoke 只消费传入值：`:1397 run_id=harness_run_id`、`:1442 harness_run_id=harness_run_id`；smoke 中已无 `derive_run_id`/`patch_set_digest`（grep 为空）。探针：4 组 `(instance_id, patch_sha, seed)` 的 F 结果与 D 的 `derive_run_id` 全部相等（`ALL_EQUAL True`）；端到端用 F 的 run_id 调 D 的 `run()` → `DECISION=PLATFORM_BLOCKED, ERROR_KIND=ENV_ERROR`，**不再抛 `HarnessCacheError`**。 |
| P1-1 评测阶段 error 未映射为 F 的 ERROR verdict | **FIXED** | smoke evaluator `:1419-1429`：`execution_error_kind is not None and verdict not in {RESOLVED, UNRESOLVED}` → `verdict_value="ERROR", resolved=None`；F `:2689-2696` 据此记 `evaluation_status=ERROR`。smoke 测试 `tests/integration/test_swebench_smoke.py:891-932` 通过并断言 `evaluation_status=="ERROR"`。可选加固（非必须）：该测试再断言 `error_class=="env_error"`。 |
| P1-2 环境/镜像身份未注入 | **FIXED** | smoke 新增 `_environment_identity_digest`（`:619-625`）与 `_image_ids_for_environment`（`:628-637`）；resume 把 `environment_identity=...` 传给 `SWEBenchRunConfig`（`:1501-1517`），evaluator 给 D 传 `artifacts=` + `image_ids=`（`:1412-1417`）；报告新增 `environment_identity.digest/verified/injected_into`（`:1554-1563`），测试 `:927-929` 断言 digest 64 位且 `verified=True`。说明：`image_ids` 到 D 的注入目前由代码路径保证，尚无单独单测直接断言入参（可选补）。 |
| P1-3 失败归因粒度不足 | **FIXED** | 报告新增一级字段 `attribution` + `attribution_detail`：环境未就绪（`:1066-1071`）、dry-run 显式空（`:1097-1100`）、主链路（`:1181-1191`）、执行异常（`:1216-1220`）、harness 异常保留 `error_kind/error_class/error_origin`（`:1152-1163`）。`_attribution`（`:1248-1324`）映射 ENV_ERROR / BUDGET_STOPPED / PATCH_INVALID / HARNESS_INTERNAL_ERROR / OFFICIAL_RESOLVED / OFFICIAL_NOT_RESOLVED / OFFICIAL_EVALUATION_ERROR，`attribution_detail` 保留原始 `prediction.status`/`harness.error_kind`。测试断言 `:547/598/707/735/763/791/812/834/858/961`。旧 `status` 仍保留粗粒度（兼容），归因已由 `attribution` 一级字段承担，不再阻塞。 |
| P1-4 默认产物路径/ArtifactStore | **FIXED** | `DEFAULT_ARTIFACT_ROOT = ROOT/"artifacts"/"swebench"`（`:56`，测试 `:549-550` 断言）；`_artifact_store`（`:640-644`）在主链路（`:435-440`）与 evaluator（`:1412-1417`）都传给 D。产物落 `<root>/runs/<run_id>/swebench_harness/`（D 证据）、`<root>/reports/<run_id>.json`、`<root>/eval/<run_id>/`、`<root>/predictions/<run_id>.jsonl`，全部位于 `artifacts/swebench/` 下。说明：run 目录多一层 `runs/`（与 Phase 10 结构一致）；验收口径"落 `artifacts/swebench/`"满足；若要求 `<root>/<run_id>` 直下需再调布局（不阻塞）。 |

### F 侧 P2 复核

| 编号 | 状态 | 证据 |
|---|---|---|
| P2-1 `real_docker` 未进信封 | **FIXED** | `_write_official_envelope` 写入 `"real_docker": evaluation.real_docker`（`swebench_runner.py:2504-2505`），恢复读取 `envelope.get("real_docker", False)`（`:2659`）；smoke 不再硬编码，而是按 `ensured + EXECUTED/REBUILT + exit_code==0` 计算（`swebench_smoke.py:1430-1437`），缓存复用保守记 False（不虚报真实 Docker）。 |
| P2-2 `_sha256_text` 不一致 | **FIXED** | F `_sha256_text` 改为普通 UTF-8 并注明与 D/A/Phase 10 统一（`swebench_runner.py:173-177`）；D/A 本来就用普通 UTF-8，三处口径已一致。 |
| P2-3 run_id 不含 instance_id（批量隐患） | **FIXED** | `harness_run_id` 摘要现在包含 `instance_id`（`:528-547`），与 D 的 `derive_run_id` 完全一致；批量时同一 patch 不同实例不会共用报告文件名。 |

### 顺带观察到的其它 P2 状态（不属本次要求，记录）

- smoke **P2-4**（`comparison_path` 只写内存）：已回写磁盘（`swebench_smoke.py:1615-1624`）→ FIXED。
- smoke **P2-10**（evaluator 绕过 normalize）：已调用 `prediction_module.normalize_patch`，且只在规范化不改变哈希时采用（`:1380-1384`）——保留身份字段的设计合理 → FIXED。
- prediction **P2-7**（mode-only patch 被拒）：探针 `old mode/new mode` 现在 **OK**；**P2-8**（hunk 内容行以 `--- a/` 开头被误判）：探针现在 **OK**（改为按 hunk 行数识别边界）。**P2-6**（rename）：仍明确拒绝，属设计选择/已知限制（建议写入 E-15 文档），不计缺陷。

### 最终复核测试证据（只读）

```
.venv\Scripts\python.exe -m pytest tests/integration/test_swebench_smoke.py tests/eval/test_swebench_subsets.py -q
  → 24 passed in 94.99s
.venv\Scripts\python.exe -m pytest tests/eval/test_swebench_runner.py -q
  → 63 passed in 256.29s
P0 equality probe（%TEMP）：4/4 case F==D，ALL_EQUAL True
P0 e2e probe（%TEMP）：用 F 的 run_id 调 D runner → PLATFORM_BLOCKED / ENV_ERROR（无 HarnessCacheError）
prediction probe（%TEMP）：mode-only OK、hunk 内容行 OK、rename 仍 REJECTED（已知限制）
```

**最终判定：可以按当前状态进入 E-17/E-19 的 Linux/WSL 真实运行；无 STILL_OPEN 的 P0/P1。**

