# Phase 11：SWE-bench Lite 集成（E-11～E-20）开发记录

> **后续（Phase 11R，2026-10-19）**：本轮的"真实镜像构建 NOT RUN / 真实 Harness NOT RUN"
> 已在 [`phase11-runtime-acceptance.md`](phase11-runtime-acceptance.md) 里补齐：Linux/WSL2
> 全量离线门禁 exit 0、`swebench==4.1.0` 官方 CLI 真实执行、一个官方 dev 实例的
> Gold + 三种负向控制真实判分。该文件同时更正本文两处口径（镜像名的 namespace 前缀、
> "补丁打不上"的官方归类）。下文保留 Phase 11 当时的事实，不作追溯修改。
>
> 本文件记录真实执行过的东西。凡是没跑过的，正文里直接写 **NOT RUN**，不写"应该可以"。
> 状态口径（与任务书 §21 一致）：`IMPLEMENTED / TESTED` 指代码与离线测试完成，
> 不等于官方 M3 验收通过。**M3 仍未达成**：固定 50/300 题的真实模型 + 官方 Harness
> 评测没有执行。

## 0. 结论摘要

| 项 | 状态 |
|---|---|
| E-11 数据集加载与 Schema | IMPLEMENTED / TESTED（真实官方数据已加载：dev 23 / test 300；loader+prediction 88 passed） |
| E-12 推理/Gold/隐藏测试强制隔离 | IMPLEMENTED / ISOLATION VERIFIED（25 passed，含 Gold Sentinel 与正向可读性） |
| E-13 Import Graph 与运行时信息隔离 | IMPLEMENTED / IMPORT TEST PASS（168 模块、入口闭包 204、违规 0） |
| E-14 官方环境与镜像适配 | IMPLEMENTED / OFFLINE TESTED（66 passed / 1 skipped 为真实 docker 只读 preflight）；真实镜像构建 **NOT RUN** |
| E-15 官方 Prediction 格式与补丁导出 | IMPLEMENTED / TESTED（含独立 review 的补丁解析边界修复） |
| E-16 官方 Harness 适配 | IMPLEMENTED / HARNESS MOCK PASS（78 passed）；真实 Harness **NOT RUN** |
| E-17 单题完整链路 Smoke | ENGINEERING IMPLEMENTED / **LIVE NOT RUN**（dry-run 端到端实测；离线 smoke 24 passed） |
| E-18 固定 30/50 子集 | 30/50 SUBSETS FROZEN（seed 20261019，交集 0） |
| E-19 断点续跑 / 三层恢复 | IMPLEMENTED / RESUME VERIFIED（63 passed，含 2 个独立进程恢复） |
| E-20 费用模型与实验计划 | EXPERIMENT PLAN COMPLETE / REAL COST CALIBRATION **PENDING** |
| M3 正式验收 | **NOT ACHIEVED**（未跑固定 50 题官方 Harness 真实评测） |

跨模块只读复核（Owner D）发现 1 个 P0 + 4 个 P1 + 10 个 P2，P0/P1 已全部修复并由 D
按最终 diff 复核确认 FIXED（详见 `docs/notes/phase11-review.md`），其中 P0 是
`SWEBenchRunner` 与官方 Harness 适配器的 `run_id` 摘要算法不一致——不修的话，真实
Linux/WSL 评测会在启动官方 harness 前 100% 失败；本轮在离线测试里抓到了它，这正是
“不要用 mock 成功冒充真实成功”的边界证据。

本轮 **0 次真实 LLM API 调用**、**0 个官方镜像构建/拉取**、**未执行 docker system prune**、
**未修改历史 Artifact 与 GLM 实验证据**。

## 1. 固定数据源与 Revision

| 项 | 值 |
|---|---|
| 数据集 | `princeton-nlp/SWE-bench_Lite`（HuggingFace） |
| Revision（固定，不静默切换） | `6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2` |
| 官方 split | dev = 23 行；test = 300 行 |
| dev parquet SHA-256 | `8312f321838051849d2fa7c6ca071244733a3e90bb517ff6ca8186f722199b5c` |
| test parquet SHA-256 | `7a21f37b8bc179c7db5beeb14e88ac538ba283455c776e6b2535bbfb6e3551b4` |
| 本地快照 | `cache/swebench/princeton-nlp__SWE-bench_Lite__<revision>/`（gitignored） |
| 快照文件 | `manifest.json` + `dev.jsonl` + `test.jsonl` |

快照由 `scripts/fetch_swebench_lite.py` 生成：按固定 revision 下载官方 parquet，校验
SHA-256 后转成 JSONL（`FAIL_TO_PASS` / `PASS_TO_PASS` 从 JSON 字符串规范化为数组），
manifest 记录 source URL、parquet hash、JSONL hash、行数、`content_sha256`。
运行时 Loader **只读快照、不联网**；manifest/JSONL 哈希不符即
`SWEBenchCacheError`，缓存缺失时明确报错而不是静默重下，revision 变化会得到不同的
缓存身份（`cache_identity`），不会沿用旧数据。

真实加载证据（本机执行）：

```
SWEBenchLoader(Path("cache/swebench")).load_dataset("test")
list_instances("test") -> 300，首条 astropy__astropy-12907
get_inference_task -> 字段 {base_commit, dataset, env_spec, environment_setup_commit,
                            instance_id, mode, problem_statement, repo, version}
get_evaluation_task -> 字段 {base_commit, dataset, environment_setup_commit, fail_to_pass,
                             gold_patch, gold_patch_sha256, instance_id, pass_to_pass,
                             repo, test_patch, test_patch_sha256, version}
validate_dataset() -> official_sizes_match=True, dev=23, test=300
```

复现命令：`python scripts/fetch_swebench_lite.py --verify-only`（离线校验），
或直接跑 `pytest tests/eval/test_swebench_loader.py`。

## 2. 推理字段与评测字段的隔离

两个**独立 Pydantic 模型**，不是同一个类加 `exclude`：

| 推理侧 `SWEBenchInferenceTask` | 评测侧 `SWEBenchEvaluationTask` |
|---|---|
| `instance_id` | `instance_id` |
| `repo` | `repo` |
| `base_commit` | `base_commit` |
| `version` | `version` |
| `environment_setup_commit` | `environment_setup_commit` |
| `problem_statement` | `dataset` |
| `dataset`（数据源身份） | `gold_patch` + `gold_patch_sha256` |
| `env_spec` | `test_patch` + `test_patch_sha256` |
| `mode="swebench"` | `fail_to_pass` / `pass_to_pass` |

除身份字段（`instance_id` / `repo` / `base_commit` / `version` /
`environment_setup_commit` / `dataset`）外**没有交集**；`patch`、`test_patch`、
`FAIL_TO_PASS`、`PASS_TO_PASS`、`hints_text` 只存在于评测侧。推理对象
`model_dump()` / `repr()` / JSON 序列化里不会出现任何 gold 字段（有逐字段测试）。
Loader 的 `get_inference_task()` 在构造时根本不读取评测字段以外的任何内容。

**推理侧 import 边界**：`codepilot.eval.swebench_task` 不 import
`codepilot.eval.swebench`（Loader + EvaluationTask）、不 import
`codepilot.eval.swebench_harness`、不 import 官方 `swebench` 包。
`codepilot.eval.assembly`（完整 M2 图装配）通过结构化 Protocol
`InferenceTaskLike` 接收任务，只要求 `task_id/issue/base_commit/repository/env_spec/mode`
五个属性，因此它既不 import SWE-bench 任务模块、也不 import 任何评测侧模块。
Import Graph 测试会对推理入口闭包做传递闭包检查（含包 `__init__` 再导出与动态
import 入口），并带“注入一条违规 import 会被抓到”的负控制。

Gold Sentinel：唯一随机 token 只植入评测侧对象/文件，然后分别尝试通过
`InferenceTask`、Workspace 文件、Read/Search 工具、Git 工具、Prompt、Artifact、
Checkpoint、恢复路径读取，全部失败才算通过；同时正向断言 Base 源码、Base 已有测试、
普通 Repository Index 仍然可读（不允许靠“全部拒绝”通过）。

## 3. Workspace 与 Git 历史隔离

- 推理 workspace 由 `WorkspaceManager.create(source=<github url>, base_commit=..., mode="fetch")`
  创建：只 fetch base 一个提交、`.git` 在 workspace 内、无 remote、`rev-list --all --count == 1`。
  `InferenceAssembly._assert_isolated` 在开跑前断言这三条；不满足直接拒绝。
- 任务身份记录（`workspace_identity.json`）记录 source/base/repository_id；
  恢复进程必须核对 base commit，不一致拒绝复用现场。
- 评测侧材料（official test patch、FAIL_TO_PASS、gold patch）从不写入 workspace；
  workspace 里的 `eval/` 目录被 `WorkspacePathPolicy` 拒绝访问（与 Phase 10 同一条防线，
  SWE-bench 复用同一份策略）。
- 缓存里的 bare clone 有完整上游历史，但它位于 workspace 之外（工具层无法读），
  而 workspace 本身只有 base 一个提交；这是 Phase 10 已验证过的隔离结构，
  SWE-bench 沿用同一实现并新增了 git 历史审计测试。

## 4. 官方环境与镜像

- 锁定官方 harness：`swebench==4.1.0`（独立 venv `.venv-swebench`）。
  - 4.1.0 的 `swebench.harness.prepare_images` 顶层 `import resource`（Unix-only），
    因此**原生 Windows 无法 import 官方包**；主 venv 不安装它，生产代码不在模块顶层
    import swebench。官方 Harness 真实执行只能在 WSL2 / Linux 里进行——这是平台限制，
    不是配置问题，预检查会明确归因为 `ENV_ERROR / RESOURCE_BLOCKED`。
- 官方环境规格冻结快照：`configs/swebench/env-specs-4.1.0.json`（来自
  `swebench.harness.constants.MAP_REPO_VERSION_TO_SPECS`，66 个 repo），运行时只读快照。
- 镜像身份（由 4.1.0 的 `TestSpec` 实测命名规则）：
  - instance：**默认是远端预构建镜像** `swebench/sweb.eval.x86_64.<instance_id 小写、`__`→`_1776_》:latest`
    （官方 CLI `-n/--namespace` 默认值 `swebench`）；`-n none` 才是本地构建形态
    `sweb.eval.x86_64.<instance_id>:latest`
  - env：`sweb.env.py.x86_64.<22 位 hash>:latest`（hash 来自 `str(env_script_list)`，离线不可复算）
  - base：`sweb.base.py.x86_64:latest`（规格带 `docker_specs` 时追加 10 位 hash）
  - 环境身份还把 `repo/base_commit/environment_setup_commit/dataset_revision/harness_version/
    spec_sha256` 一起哈希成 `environment_hash`，不能只靠 repo 名判断复用。
  - ⚠ Phase 11 当初只写了无 namespace 的本地构建名，与官方默认查的镜像不是同一个；
    已在 Phase 11R 修复（见 [`phase11-runtime-acceptance.md`](phase11-runtime-acceptance.md) §9 P1-1）。
- 预检查：Docker daemon、Linux 容器模式、镜像存在性、磁盘余量、harness 版本、平台可执行性；
  不足返回 `ENV_ERROR` / `RESOURCE_BLOCKED`，不把环境失败记成模型失败。
  （Phase 11 当时按"C: 空闲约 29 GB"评估为紧张；Phase 11R 实测 Docker 数据落在
  `D:\Docker\wsl\disk\docker_data.vhdx`，D 盘空闲 236 GB，评估结论作废。）
- 清理：只清理带 `codepilot.*` 标签的自家临时容器；不执行 `docker system prune`、
  不删除/修改其他项目镜像。
- **真实镜像构建/官方镜像拉取：NOT RUN**。本机 258 个镜像占 43.79GB，其中没有
  swebench 镜像；C: 余量约 29GB，官方实例镜像单个通常 1GB+，本轮按任务书要求停在
  离线适配 + Mock 验收，不做不受控下载。
- 沙箱兼容：推理阶段仍走既有 `DockerSandbox`（网络隔离、cap-drop、只读根等策略不变）。
  官方镜像直接复用作为推理环境**未实现**（deferred）；当前按官方规格派生 `EnvSpec`
  交给既有 sandbox，差异已记录在 `docs/05` 的 Deferred Acceptance。

## 5. Prediction 与官方 Harness

- predictions.jsonl 每行严格三字段（4.1.0 实测 key）：
  `{"instance_id": ..., "model_name_or_path": ..., "model_patch": ...}`
- 补丁来源：CodingAgent 在隔离 workspace 产出、经 `PatchValidator` 处理后的 patch；
  禁止 gold patch、隐藏测试拼接、评测器返回的补丁、FakeLLM 预置 gold。
- 标准化：标准 Git unified diff；拒绝绝对路径 / `..` / `.git/` / `eval/`；保留缩进与
  空白；支持多文件、新建、删除；排除 Phase 6 复现目录文件；记录 patch sha256。
- 原子写（同目录临时文件 + `os.replace`）、去重、顺序稳定、断点恢复、Run ID 与 model
  identity 绑定、manifest 记录每条 instance 的 status 与 patch hash。
- 空补丁：默认不写官方预测行（记为 `EMPTY_PATCH`），不当作 resolved，也不从分母里
  悄悄移除；manifest 与报告都会显示它。
- 官方 Harness 命令（4.1.0 实际 CLI，经 `.venv-swebench` 核实）：
  `python -m swebench.harness.run_evaluation -d <dataset> -s <split> -i <ids...>
   -p <predictions.jsonl> --max_workers <n> -id <run_id> -t <timeout> --report_dir <dir>`
- 报告：`<model_name_or_path 中 / → __>.<run_id>.json`，schema_version=2，字段含
  `total_instances/submitted_instances/completed_instances/resolved_instances/
  unresolved_instances/empty_patch_instances/error_instances/completed_ids/
  incomplete_ids/empty_patch_ids/submitted_ids/resolved_ids/unresolved_ids/error_ids`。
- 判分语义映射：`resolved` / `unresolved` / `patch apply failed` / `error` / 空补丁 /
  缺失预测 / 未评测，分别映射到 `OfficialVerdict`。**CodePilot 内部 `m2_verified`
  永远不等于 `official_resolved=True`**，两者不一致本身就是报告里的一个指标。
  真实执行更正：官方 4.1.0 对"补丁打不上"抛 `EvaluationError`，**不写** 单实例
  `report.json`，只在汇总里进 `error_ids`（不算 unresolved）；该形态只能由
  `run_instance.log` 里的 `>>>>> Patch Apply Failed` 识别，Phase 11R 已据此修复归因。
- 评测缓存负控制：缓存身份 = patch hash + dataset revision + harness version + 镜像身份 +
  run_id；同一 instance 的 Patch A 与 Patch B 不共享未经验证的结论；报告缺失/损坏/身份
  不符一律不复用。有专门测试构造“旧报告被新 patch 误复用”的负控制。
- **run_id 单一真源（P0 修复）**：官方 Harness 的 run_id 由
  `derive_run_id({instance_id: patch_sha256}, seed)` 生成（摘要 = `patch_set_digest[:12]`），
  `SWEBenchRunner.harness_run_id(instance_id, patch_sha256, seed)` 直接调用它；
  有测试直接调用适配器的 `_enforce_run_id_digest` 证明它接受该 run_id，并用旧算法
  证明会被拒绝。修复前 F/D 两套算法不一致，真实评测会在启动官方 harness 前
  `HarnessCacheError`（离线测试已覆盖）。
- `ORACLE_CONTROL`：允许在**评测侧**用 gold patch 调用官方 Harness 做正向控制，
  但必须显式标记、不进入 `predictions.jsonl`、不进入推理、不计入模型 Resolved Rate。
  本轮未执行该控制（需要真实镜像），只完成实现与离线测试。

## 6. 单题 Smoke（E-17）

入口：`python scripts/swebench_smoke.py --instance-id <id>`，以及统一 CLI
`codepilot bench-swebench --instance-id <id> [--dry-run|--live-llm]`（CLI 是薄壳，
执行逻辑只有一份）。

链路（与任务书 §11.2 一致）：

```
Loader → SWEBenchInferenceTask → WorkspaceManager(base commit) → 环境适配
      → 完整 M2 LangGraph（InferenceAssembly，Phase 9 同一张图）
      → PatchValidator → predictions.jsonl → 官方 Harness 适配器 → 官方报告 → 汇总
```

- `--dry-run`：不调 LLM、不建镜像，验证 Loader/环境解析/装配/Prediction 写出/命令拼装/
  Artifact 路径/预算与费用检查/隔离断言。
- `--repeats 2` 或独立两次调用：Run A / Run B 各自 run_id、checkpoint、patch、prediction、
  官方评测、usage、cost、artifact；不预设相同 seed 的模型输出一致。
- 真实 GLM 运行：**LIVE NOT RUN**（本轮禁止调用付费 API）。
- 离线端到端证据见 `tests/integration/test_swebench_smoke.py`（FakeLLM + FakeSandbox +
  Mock Harness，真实 M2 图），覆盖任务书 §16.E 的十种情形。

## 7. 固定子集（E-18）

- 目录：`configs/subsets/`
  - `swebench-lite-dev30.json`（30 条，自定义开发子集）
  - `swebench-lite-test50.json`（50 条，冻结测试子集）
  - `swebench-lite-official-dev23.json`（官方 dev 23 条，独立参考）
  - `manifest.json`（dataset/revision/seed/算法/文件 hash/生成时间/口径说明）
- **自定义划分与官方 split 的区别**：官方 SWE-bench Lite 只有 dev 23 / test 300。
  本项目的 dev30/test50 都是从官方 **test 300** 里固定抽样的自定义子集，不是官方 dev，
  也不能声称与公开榜单的 300 题口径相等；manifest 与报告都会写明。
- 抽样：seed `20261019`，`instance_id` 升序稳定排序，按 repo 分层轮转、再按 version
  轮转，前 30 为 dev30、随后 50 为 test50，两组交集 0；不使用任何模型结果挑选。
- 冻结机制：文件 hash 写进 manifest；测试断言 30/50/无交集/ID 真实存在/无重复/可重复
  生成/换 seed 会变化/revision 不符不静默沿用。

## 8. 恢复、预算与报告（E-19 / E-20）

- 三层恢复，**不是**一个布尔 `--resume`：
  - `TASK`：已完成且证据完整的 instance 直接跳过；
  - `GRAPH`：未完成的 LangGraph 从 SQLite checkpoint 续跑（复用 Phase 9 机制）；
  - `EVALUATION`：patch hash + run_id + dataset revision + harness version + 镜像身份
    全部一致、报告完整时才复用官方结论。
- 运行身份：dataset / revision / subset hash / instance / repo / base_commit /
  provider-model / config hash / code commit / seed / patch hash / harness version。
- Workspace 恢复校验：存在、base 一致、patch hash 一致、索引可用、artifact 可读、
  checkpoint schema 兼容、配置一致；不一致时拒绝复用或按证据安全重建。
- 外部操作沿用 Phase 10 的 Operation Journal / SQLite Checkpointer / ArtifactStore，
  口径是 `at-most-once-with-verified-evidence`，**不宣称 exactly-once**。
- 预算：恢复不重复扣 token / 测试数 / attempt / 已确认费用；未知费用保持 unknown；
  单任务预算耗尽不停止其他任务（除非整批预算耗尽）。
- 报告同时给两个分母：全部目标任务口径 vs 实际完成官方评测口径；
  Harness/EVAL 错误不记成模型未解决，也不从覆盖率里隐藏。
- 费用模型：`docs/experiment-plan.md`。真实 10 题成本校准 **NOT RUN**，E-20 状态
  `ENGINEERING_IMPLEMENTED / REAL_COST_CALIBRATION_PENDING`。历史 Synthetic 39 题的
  GLM 真实用量只作工程参考，不冒充 SWE-bench 平均成本。

## 9. 测试与质量门禁

新增测试（Phase 11，9 个文件 345 项；本轮全部真实执行）：

| 文件 | 覆盖 | 结果 |
|---|---|---|
| `tests/eval/test_swebench_loader.py` + `test_swebench_prediction.py` | E-11 / E-15 | 88 passed |
| `tests/eval/test_swebench_isolation.py` + `test_isolation.py` | E-12 / E-13 | 25 passed（真实 M2 图 + Gold Sentinel 负控制 + Import Graph） |
| `tests/sandbox/test_swebench_env.py` | E-14 | 66 passed / 1 skipped（skip = 真实 docker 只读 preflight，会话沙箱禁止 Python 访问 daemon） |
| `tests/eval/test_swebench_harness.py` | E-16 | 78 passed（全 Mock subprocess；夹具来自真实 4.1.0 `--help`/argparse/报告） |
| `tests/integration/test_swebench_smoke.py` + `tests/eval/test_swebench_subsets.py` | E-17 / E-18 | 24 passed（§16.E 十种情形全覆盖） |
| `tests/eval/test_swebench_runner.py` | E-19 | 63 passed（含 2 个独立进程 `os._exit` 恢复） |

静态门禁（本机，真实输出）：

```
ruff check .                  -> All checks passed!
ruff format --check .         -> 371 files already formatted
mypy src                      -> Success: no issues found in 168 source files
```

全量离线门禁 `pytest -m "not docker and not llm" --cov --cov-report=term-missing`
（本机真实执行，未裁剪）：

```
collected 3283 items / 45 deselected / 3238 selected
= 297 failed, 2927 passed, 14 skipped, 2 warnings in 1917.08s (0:31:57) =
Required test coverage of 80.0% reached. Total coverage: 87.12%
exit code: 1（失败全部为下述环境阻塞，见分类证据；覆盖率门禁本身通过）
```

失败分类与证据（**不是本次改动引入的回归**）：

1. **297 个失败全部落在 46 个既有测试文件里；Phase 11 新增的 9 个测试文件
   0 失败**（345 项全部通过）。
2. 失败日志含 **329 处** `NtCreateDirectoryObject(\BaseNamedObjects\msys-2.0S5-...)`
   与 **327 处** `Could not read from remote repository` / `git clone 失败`：
   本会话的进程沙箱禁止 Python 子进程拉起 Git-for-Windows 的 msys `sh.exe`
   （从 pwsh 直接 `git clone` 正常，从 `python -c subprocess.run(["git","clone",...])`
   必失败）。依赖 `WorkspaceManager` 克隆本地 fixture 的测试因此失败。
3. **基线对照**：同一命令在 Phase 10 基线提交 `57e5187` 的独立 worktree 上复现
   （`tests/unit/repo/test_workspace.py`：基线 **48 failed / 14 passed**，当前分支
   **48 failed / 14 passed**）；另取 83 个失败节点（跨 46 个文件，每文件最多 2 个）
   在基线 worktree（`PYTHONPATH` 指向基线 `src`）上重跑：**82/83 同样失败**；剩下
   1 个 `tests/eval/test_synthetic.py::test_materialize_excludes_eval_and_keeps_base_source`
   在基线也通过、在当前分支 `--lf` 重跑同样通过，属并发/顺序相关的既有偶发，不是回归。
4. 另有 13+ 个失败来自**本会话 PATH 中没有 `ripgrep`**（`rg` 不存在；
   `shutil.which("rg") is None`），属于测试对真实 ripgrep 二进制的依赖。
5. `--lf` 重跑这 297 个失败节点：**296 failed / 1 passed**（同一批环境原因，
   1 个依赖时序的用例偶发通过），说明失败是可复现的环境问题而非随机代码缺陷。

因此本轮结论**不是**"全量门禁 exit 0"：ruff / format / mypy 三项 exit 0，覆盖率门禁
（≥80%）通过，但 pytest 因宿主机沙箱与缺失 ripgrep 有 297 个既有用例失败。换到正常
开发机（有 ripgrep 且不受该会话沙箱限制）时这些用例在 Phase 10 全部通过（历史门禁
2665 passed）；本轮没有为了凑绿而 skip、放宽断言或删除用例。

## 10. Git

- 分支：`codex/phase11-swebench-lite`（从 Phase 10 收口提交 `57e5187` 创建）
- 远端：**未 push**；无 PR / merge / reset / rebase / amend / force push
- Phase 10 收口：`57e5187 fix(benchmark): finalize synthetic fixture remediation`
- Phase 11 分组提交（本地，按工作组）：

| 提交 | 内容 |
|---|---|
| `c04f9f5` | E-11 数据加载：pinned snapshot、双类型、provenance |
| `a0a1f3e` | E-15 官方 Prediction：patch 校验、七态、原子写与恢复 |
| `1f607f4` | E-12/E-13 Gold Sentinel、运行时审计与 Import Graph |
| `b216071` | E-14 官方环境规格、镜像身份、资源预检查 |
| `eef46a1` | E-16 官方 Harness Adapter：命令、报告、缓存身份、ORACLE_CONTROL |
| `70e7938` | E-19/E-20 三层恢复、预算与实验计划 |
| `d685828` | E-17/E-18 bench-swebench CLI、单题 Smoke、30/50 冻结子集 |
| 本提交 | 文档收口（04/05/notes/experiment-plan/contract/review） |

- 文档收口提交：ee8cc6；其后仅追加本条 Git 状态记录，故最终 HEAD 为该记录提交（git log -1）。
- 工作树：最终 git status --short 仅剩既有未跟踪目录 .playwright-mcp/，其余干净；`.playwright-mcp/` 是此前既有的未跟踪目录，
  `.venv-swebench/`、`cache/`、`artifacts/`、`.env` 均由 `.gitignore` 忽略。
- 未跟踪/忽略材料不含 API Key、`.env`、原始 Gold 审查材料或大型 Artifact。


## 11. 未执行 / 阻断项

1. **真实官方 Harness 评测（50/300 题）：NOT RUN**。Phase 11R 已把"能不能跑"这一步打通
   （一个官方 dev 实例的 Gold + 三种负向控制真实判分，见
   [`phase11-runtime-acceptance.md`](phase11-runtime-acceptance.md) §6），批量仍未执行；
   题量与费用需要单独授权。
2. **真实 GLM 推理：NOT RUN**。本轮禁止付费 API；E-17 保持 `LIVE NOT RUN`。
3. **官方镜像作为推理环境复用：DEFERRED**。当前推理沙箱使用官方规格派生的 `EnvSpec`，
   与官方镜像环境可能有依赖差异；真实跑批前需要先验证两者一致性。
4. **10 题真实费用校准：NOT RUN**。E-20 的费用模型只有工程实现与历史数据参考。
5. Phase 10 的 F1–F4 低危覆盖缺口保留，未在本轮扩范围修改（不阻塞 Phase 11 集成）。
