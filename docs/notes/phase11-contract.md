# Phase 11 内部接口契约（E-11～E-20）

> 这份文件是并行开发的**唯一接口真源**。各模块按此实现，不要擅自改名/改字段；
> 需要变更时先通知 Lead，由 Lead 统一改契约并告知所有 owner。
> 本文件作为设计记录保留；交付记录与真实验证结果见 `docs/notes/phase11.md`，
> 跨模块只读复核与 P0/P1 修复见 `docs/notes/phase11-review.md`。

## 0. 固定事实（不要重新下载/换版本）

| 项 | 值 |
|---|---|
| 数据集 | `princeton-nlp/SWE-bench_Lite` |
| Revision（固定） | `6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2` |
| 官方 split | dev = 23 行，test = 300 行 |
| dev parquet sha256 | `8312f321838051849d2fa7c6ca071244733a3e90bb517ff6ca8186f722199b5c` |
| test parquet sha256 | `7a21f37b8bc179c7db5beeb14e88ac538ba283455c776e6b2535bbfb6e3551b4` |
| 官方 Harness | `swebench==4.1.0`（安装在 `.venv-swebench`，**不进主 venv**） |
| 官方环境规格快照 | `configs/swebench/env-specs-4.1.0.json`（261KB，来自 `swebench.harness.constants.MAP_REPO_VERSION_TO_SPECS`） |
| 数据集快照（已生成，gitignored） | `cache/swebench/princeton-nlp__SWE-bench_Lite__<revision>/manifest.json|dev.jsonl|test.jsonl` |

**swebench 4.1.0 在原生 Windows 上不可 import**：`swebench/harness/prepare_images.py` 顶层
`import resource`（Unix-only）。因此：

- 生产代码**不得**在模块顶层 import swebench；只能通过 `importlib.util.find_spec` 探测，
  或读取冻结快照。
- 需要读取官方 Python 对象时，用 `.venv-swebench` 子进程，并在子进程里先注入
  `sys.modules["resource"]` 桩（见 `cache/verify/gen_assets.py` 的做法）。
- 真实官方 Harness 执行在原生 Windows 上属于 `ENV_ERROR / RESOURCE_BLOCKED`，
  归因必须写清楚（WSL2 或 Linux 才可执行）。

数据集快照 file format：

- `manifest.json`：`dataset_name`、`revision`、`source`、`official_split_sizes`、
  `splits.{dev,test}.{rows,parquet_filename,parquet_sha256,parquet_bytes,jsonl_sha256,jsonl_bytes,source_url}`、
  `content_sha256`、`schema_version=1`。
- `<split>.jsonl`：官方字段原样，`FAIL_TO_PASS`/`PASS_TO_PASS` 已从 JSON 字符串规范化为
  **字符串数组**；每行一个 instance，key 排序稳定。

## 1. 文件所有权（写作用域，互斥）

| Owner | 只允许写 |
|---|---|
| A（loader） | `src/codepilot/eval/swebench_task.py`、`src/codepilot/eval/swebench.py`、`src/codepilot/eval/swebench_prediction.py`、`tests/eval/test_swebench_loader.py`、`tests/eval/test_swebench_prediction.py`、`tests/fixtures/swebench/`、`scripts/fetch_swebench_lite.py` |
| B（isolation） | `src/codepilot/eval/swebench_isolation.py`、`tests/eval/test_isolation.py`、`tests/eval/test_swebench_isolation.py`、`tests/fixtures/swebench_isolation/` |
| C（environment） | `src/codepilot/sandbox/swebench_env.py`、`src/codepilot/sandbox/env_spec.py`（只做向后兼容的最小修改）、`tests/sandbox/test_swebench_env.py` |
| D（harness） | `src/codepilot/eval/swebench_harness.py`、`tests/eval/test_swebench_harness.py`、`tests/fixtures/swebench_harness/` |
| E（smoke+subsets） | `scripts/swebench_smoke.py`、`scripts/make_swebench_subsets.py`、`configs/subsets/`、`tests/eval/test_swebench_subsets.py`、`tests/integration/test_swebench_smoke.py` |
| F（resume+plan） | `src/codepilot/eval/swebench_runner.py`、`tests/eval/test_swebench_runner.py`、`docs/experiment-plan.md` |
| Lead | `src/codepilot/eval/assembly.py`、`src/codepilot/cli.py`、`docs/04-*`、`docs/05-*`、`docs/notes/phase11.md`、`README` 相关、`.gitignore`、`pyproject.toml` |

任何跨文件修改先找 Lead。**不要**执行任何 `git add/commit/switch/checkout`。

## 2. 数据模型（Owner A，`swebench_task.py`）

```python
DATASET_NAME = "princeton-nlp/SWE-bench_Lite"
DATASET_REVISION = "6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2"
HARNESS_VERSION = "4.1.0"
OFFICIAL_SPLIT_SIZES = {"dev": 23, "test": 300}

class SWEBenchDatasetRef(BaseModel):   # frozen, extra="forbid"
    name: str = DATASET_NAME
    revision: str                      # 40/64 位 hex，必须 == 常量或显式校验通过的快照 revision
    harness_version: str = HARNESS_VERSION
    split: Literal["dev", "test"]

class SWEBenchInferenceTask(BaseModel):  # frozen, extra="forbid"
    instance_id: str          # ^[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-\d+$ 类似官方格式，需校验
    repo: str                 # "owner/name"
    base_commit: str          # 40 hex
    problem_statement: str    # 非空
    version: str              # 官方 version 字段，非空
    environment_setup_commit: str
    dataset: SWEBenchDatasetRef
    env_spec: EnvSpec
    mode: Literal["swebench"] = "swebench"

    @property
    def task_id(self) -> str: return self.instance_id
    @property
    def issue(self) -> str: return self.problem_statement
    @property
    def repository(self) -> str:  # assembly 用；远端 URL
        return f"https://github.com/{self.repo}.git"
```

**禁止**出现在 `SWEBenchInferenceTask` 的字段/property/`__repr__`/序列化中：
`patch`、`gold_patch`、`test_patch`、`FAIL_TO_PASS`、`PASS_TO_PASS`、`hints_text`、
`environment_setup_commit` 之外任何评测字段、`EvaluationTask` 引用。
（`environment_setup_commit` 是官方环境身份的一部分，允许；`hints_text` 一律不给。）

`SWEBenchEvaluationTask`（A，`swebench.py`）：`instance_id, repo, base_commit, version,
environment_setup_commit, dataset: SWEBenchDatasetRef, gold_patch, test_patch,
fail_to_pass: tuple[str,...], pass_to_pass: tuple[str,...], gold_patch_sha256,
test_patch_sha256`。与推理对象**字段集合不相交**（instance_id/base_commit/repo/version/
environment_setup_commit/dataset 允许同时存在，因为它们是身份字段；其余严格互斥）。

## 3. Loader（Owner A，`swebench.py`）

```python
class SWEBenchDatasetError(ValueError): ...
class SWEBenchCacheError(SWEBenchDatasetError): ...

class SWEBenchLoader:
    def __init__(self, snapshot_root: Path, *, dataset_name: str = DATASET_NAME,
                 revision: str = DATASET_REVISION) -> None
    def load_dataset(self, split: Literal["dev","test"]) -> None
    def list_instances(self, split) -> tuple[str, ...]              # 稳定排序
    def get_inference_task(self, split, instance_id) -> SWEBenchInferenceTask
    def get_evaluation_task(self, split, instance_id) -> SWEBenchEvaluationTask
    def get_dataset_manifest(self) -> DatasetManifest
    def validate_dataset(self) -> DatasetValidation             # 全量结构/重复/字段校验
```

- 缓存有效时**完全离线**；manifest/JSONL hash 不符 → `SWEBenchCacheError`（不静默重下）。
- 重复 instance_id、非法 repo（必须 `owner/name` 且不含空格/`..`）、非法 base_commit、
  缺字段/类型错 → `SWEBenchDatasetError`，fail closed。
- revision 变化 → 缓存身份（snapshot 目录）不同；不得回退到其他 revision。

## 4. Prediction Writer（Owner A，`swebench_prediction.py`）

官方 JSONL 每行三字段（4.1.0 实际 key，已核实）：

```json
{"instance_id": "...", "model_name_or_path": "...", "model_patch": "diff --git ..."}
```

```python
class PredictionRecord(BaseModel):   # 仅官方三字段（写出时）
    instance_id: str
    model_name_or_path: str
    model_patch: str

class PredictionStatus(StrEnum):
    PATCH_READY / EMPTY_PATCH / INVALID_PATCH / MODEL_OUTPUT_INVALID /
    WORKFLOW_STOPPED / ENV_ERROR / BUDGET_EXHAUSTED

class PredictionEntry(BaseModel):    # 内部记录：status + hash + 来源 run_id/patch_sha256
    ...

class PredictionWriter:
    def __init__(self, path: Path, *, model_name_or_path: str, run_id: str,
                 dataset: SWEBenchDatasetRef, allow_existing: bool = True)
    def add(self, entry: PredictionEntry) -> None
    def write(self) -> PredictionManifest   # 原子写、去重、顺序稳定、断点恢复
    @staticmethod
    def load(path: Path) -> list[PredictionRecord]
    def verify_integrity(self) -> PredictionManifest
```

- `add` 只接受**经 PatchValidator 处理过**的 patch；空 patch 记录为 `EMPTY_PATCH` 且**不写进**
  官方 jsonl（默认），由 manifest 记录；`include_empty=False` 参数可强制写出空 patch 行。
- 补丁规范化：复用 DA-009 测试交付策略（排除 Phase 6 `tests/` 复现文件），拒绝绝对路径、
  `..`、`.git/`、`eval/`；保持缩进/空白；计算 sha256；支持多文件/新建/删除。
- 原子写：同目录 `*.tmp` + `os.replace`；重复 instance 以**后写覆盖**并计数；manifest 记录
  每个 instance 的 status、patch hash 与来源 run_id。

## 5. 官方环境适配（Owner C，`sandbox/swebench_env.py`）

```python
class SWEBenchEnvironmentError(RuntimeError):
    kind: str   # ENV_ERROR | RESOURCE_BLOCKED | UNSUPPORTED | BUILD_FAILED

class SWEBenchEnvironmentIdentity(BaseModel):   # frozen
    instance_id: str; repo: str; base_commit: str; version: str
    environment_setup_commit: str; dataset_revision: str; harness_version: str
    python_version: str; env_image_key: str; instance_image_key: str; base_image_key: str
    spec_sha256: str; environment_hash: str       # 全部身份的 sha256

class SWEBenchEnvironmentSpec:      # 官方规格解析（读冻结快照，不 import swebench）
    @classmethod
    def load(cls, path: Path = DEFAULT_SPEC_SNAPSHOT, *, harness_version=HARNESS_VERSION) -> Self
    def spec_for(self, repo: str, version: str) -> Mapping[str, Any]
    def env_spec_for(self, repo, version) -> EnvSpec      # 含 pip_packages → install
    def requirements(self, repo, version) -> tuple[str,...]

class SWEBenchEnvironmentAdapter:
    def __init__(self, *, docker: DockerBackend | None, spec_snapshot: Path, cache_dir: Path,
                 allow_build: bool = False, disk_limit_gb: float | None = None)
    def identity(self, task: SWEBenchInferenceTask) -> SWEBenchEnvironmentIdentity
    def preflight(self, *, required_instance: str | None = None) -> PreflightReport
    def ensure(self, identity) -> EnvironmentHandle     # exists→reuse(校验)；缺失且 !allow_build→RESOURCE_BLOCKED
    def cleanup_owned(self) -> tuple[str,...]           # 只清理 codepilot-* 标注的临时容器
```

- `DockerBackend` 抽象出 `daemon_ok/ostype/list_images/inspect_image/build/pull/disk_free_gb`，
  默认实现用 `docker` SDK；测试用 Fake。**不允许** `docker system prune`、删其他项目镜像。
- `preflight` 检查：daemon、Linux 容器模式、镜像是否存在、磁盘余量、harness 版本、
  平台（Windows 原生 → harness 不可执行）、WSL 可用性。
- `env_spec_for` 必须处理 4.1.0 规格里的 `pip_packages`（EnvSpec.install 追加
  `python -m pip install ...`）与 `install`/`pre_install`/`test_cmd`。
- 沙箱集成：`SWEBenchSandboxFactory` 只负责把 EnvSpec 交给现有 DockerSandbox；
  不得绕过 DockerSandbox 的隔离规则；官方镜像复用未实现时明确记录 `deferred`。

## 6. 隔离（Owner B，`swebench_isolation.py`）

```python
GOLD_SENTINEL = "CP-GOLD-SENTINEL-<run_id>-<instance_id>"   # 唯一、随机、不可猜

class IsolationViolation(RuntimeError): ...

class GoldSentinel:
    def __init__(self, token: str) -> None
    @classmethod
    def generate(cls) -> GoldSentinel
    def plant_in_evaluation_task(self, task) -> ...   # 只在评测侧对象/文件中
    def assert_absent(self, text: str, *, where: str) -> None

def audit_inference_request(prompt: str, *, where: str = "prompt") -> None
def audit_workspace(workspace: Path, *, sentinel: GoldSentinel) -> None      # 含 symlink/junction/UNC
def audit_git_history(workspace: Path, *, base_commit: str) -> None           # 仅 base 可达、无 remote/tag/ref
def audit_tool_registry(registry, *, sentinel) -> None
def audit_artifact_tree(root: Path, *, sentinel) -> None
def audit_checkpoint(path: Path, *, sentinel) -> None
def build_import_graph(package_root: Path) -> dict[str, set[str]]
def assert_inference_import_closure(graph, *, forbidden_prefixes) -> None
```

- 必须真做：workspace 内容、git 可达提交、工具注册表、prompt、artifact、checkpoint、
  恢复路径；不能"一律拒绝读文件"。
- 允许：Base 源码可读、Base 已有测试可读、Repository Index 不失效。
- `tests/eval/test_isolation.py`：Import Graph（直接/间接/相对/`__init__` 再导出/动态入口）
  + 运行时装配检查（用 FakeSandbox/FakeLLM，不调真实 LLM）+ Gold Sentinel 负控制。

## 7. 官方 Harness 适配（Owner D，`swebench_harness.py`）

```python
class HarnessConfig(BaseModel):        # frozen
    predictions_path: Path; run_id: str; report_dir: Path; max_workers: int = 4
    timeout_seconds: int = 1800; dataset_name: str = DATASET_NAME; split: str = "test"
    instance_ids: tuple[str,...] = (); harness_python: Path | None = None
    harness_version: str = HARNESS_VERSION; model_name_or_path: str

class HarnessCommand(BaseModel):
    argv: tuple[str,...]; cwd: Path; env: dict[str,str]; expected_report: Path

class OfficialVerdict(StrEnum):
    RESOLVED / UNRESOLVED / PATCH_APPLY_FAILED / EMPTY_PATCH / ERROR / MISSING / NOT_EVALUATED

class HarnessResult(BaseModel):
    verdict: OfficialVerdict; instance_id: str; resolved: bool | None
    report_path: Path | None; report_sha256: str | None; error_kind: str | None
    error_detail: str | None; runtime_seconds: float; command: HarnessCommand
    cache_identity: HarnessCacheIdentity     # patch_hash + run_id + dataset_revision + harness_version + image ids

class OfficialHarnessRunner:
    def __init__(self, config: HarnessConfig, *, runner: SubprocessRunner | None = None,
                 platform: PlatformInfo | None = None) -> None
    def build_command(self, *, patch_hashes: Mapping[str,str]) -> HarnessCommand
    def run(self) -> HarnessExecutionResult        # 子进程 + 超时 + exit code + 报告解析
    @staticmethod
    def parse_report_file(path: Path) -> HarnessReport
```

- 命令必须基于锁定版 `swebench==4.1.0` 实际 CLI（`-d/-s/-i/-p/-id/--max_workers/-t/--report_dir`），
  由 Owner D 在 `.venv-swebench` 里用 `--help` 核实并写进测试夹具。
- 报告文件名 `<model_name_or_path 中 / → __>.<run_id>.json`，字段（schema_version=2）：
  `total_instances, submitted_instances, completed_instances, resolved_instances,
  unresolved_instances, empty_patch_instances, error_instances, completed_ids,
  incomplete_ids, empty_patch_ids, submitted_ids, resolved_ids, unresolved_ids,
  error_ids`。
- 缓存身份：patch_hash 不同 → 新 run_id（`run_id = f"cp-{patch_set_sha256[:12]}-{seed}"` 形式），
  禁止沿用旧报告；报告缺失/损坏/身份不符 → 不复用并明确归因。
- 平台：Windows 原生 → `ENV_ERROR`（harness 依赖 `resource`，不可 import），
  命令仍可构造用于 dry-run；WSL/Linux 通过 `wsl.exe -e ...` 或直接执行（可配置）。
- 不把内部 `m2_verified` 当 `official_resolved=True`。

## 8. 单题 Smoke（Owner E，`scripts/swebench_smoke.py`）

- 参数：`--instance-id --split --revision --provider --model --budget --seed
  --temperature --max-output-tokens --timeout --docker-cpus --docker-memory
  --artifact-root --resume --dry-run --live-llm`（`--live-llm` 默认 False，未授权不得调真实 API）。
- 调用链必须是：Loader → InferenceTask → Workspace → Environment → **真实 M2 图**
  (InferenceAssembly) → PatchValidator 结果 → PredictionWriter → OfficialHarnessRunner → Report。
- `--dry-run`：不调 LLM、不建镜像，只验证装配、命令拼装、产物路径、预算/费用检查、隔离。
- 双次运行 `--run-label A|B` 或 `--repeats 2`：独立 run_id/checkpoint/prediction/eval/usage/artifact。
- 离线测试用 FakeLLM + FakeSandbox + MockHarness，真实 GLM 不跑，状态 `LIVE_NOT_RUN`。

## 9. 子集（Owner E，`configs/subsets/`）

- `swebench-lite-dev30.json`、`swebench-lite-test50.json`、`manifest.json`。
- 来源：官方 **test** split 300 条（不是官方 dev），固定 seed `20261019`。
- 规则：`instance_id` 升序排序、按 repo 分层轮转抽样、两组不重叠、避开近重复
  （同 repo 同 version 连续编号的相邻任务尽量分散）、不被单仓库主导；
  **不得**按 GLM 成功率挑选。官方 dev 23 条保留为 `swebench-lite-official-dev23.json` 参考。
- manifest 记录：dataset/revision、seed、算法、文件 sha256、instance 列表 hash、
  生成时间、官方 split 与自定义划分的区别说明。
- `scripts/make_swebench_subsets.py` 必须可重复生成同样结果（从快照读）。

## 10. Runner / 恢复（Owner F，`swebench_runner.py`）

```python
class ResumeLevel(StrEnum): TASK / GRAPH / EVALUATION
class RunIdentity(BaseModel):   # dataset + revision + subset hash + instance_id + repo +
                                # base_commit + provider/model + config_hash + code_commit +
                                # seed + patch_hash + harness_version
class SWEBenchRunner:
    def __init__(..., loader: SWEBenchLoader, inference_factory: Callable[..., InferenceAssembly],
                 evaluator: EvaluationCallable, journal_path: Path, artifact_root: Path)
    def run(self, instance_ids: Sequence[str], *, seed: int, resume: ResumeLevel) -> RunReport
```

- 复用 Phase 10 `BenchmarkRunner`/`ResultJournal`/`CostLedger` 的语义（TaskRecord 模式），
  不重复实现 journal；若必须扩展，保持向后兼容。
- 三层恢复：任务级（已完成且证据完整→跳过）、图级（checkpoint 续跑）、评测级
  （patch hash + run_id + 报告完整才复用）。
- 预算/费用：恢复不重复计费；未知费用保持 unknown。
- 报告指标：dispatched/completed_inference/valid_predictions/officially_evaluated/
  resolved/unresolved/harness_error/env_error/evaluation_coverage/official_resolved_rate/
  token/cost/runtime/stop_reason/patch_validity/model_claimed_success。
  同时给"全部目标任务口径"和"实际完成官方评测口径"两个分母。

## 11. 通用工程约束

- Python 3.12；`from __future__ import annotations`；ruff line-length 100；mypy strict。
- 注释/文档字符串用中文（与现有代码一致），解释"为什么"，不要写空话。
- 测试默认离线：不联网、不调真实 LLM、不构建 Docker 镜像；需要 Docker 的测试标
  `@pytest.mark.docker` 并自动 skip；不新增规避性 skip。
- 复用现有类型：`EnvSpec`、`PatchValidator`、`ArtifactStore`、`WorkspaceManager`、
  `ToolRegistry`、`LLMProvider`、`RepairBudget` 等，不要重造。
- 不要修改 `tests/eval/test_leakage.py`、`tests/eval/test_runner.py` 等既有测试。
- 每次完成一个模块，跑定向测试：`.venv\Scripts\python.exe -m pytest <your tests> -x -q`。
