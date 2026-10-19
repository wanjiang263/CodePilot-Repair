# Phase 11R：Runtime Acceptance（真实 Linux/WSL2 + 官方 Harness 验收记录）

> 分支 `codex/phase11-runtime-acceptance`，起点 `d9687c1`（Phase 11 E-11～E-20 收口）。
> 本轮只做验收与针对性缺陷修复：**0 次真实 LLM 调用、0 题批量实验**。
> 凡没跑过的都写 NOT RUN；跑过的都给命令与产物路径。
> 主记录见 [`phase11.md`](phase11.md)，跨模块 review 见 [`phase11-review.md`](phase11-review.md)。

## 0. 结论摘要

| 验收项 | 状态 | 关键证据 |
|---|---|---|
| 全量离线门禁（Linux/WSL2） | **PASS（exit 0）** | §3：ruff / format / mypy / pytest 四项同环境全 0，覆盖率 ≥80% 达标 |
| 297 项 pytest 失败归因 | **ENVIRONMENT_BLOCKED（已证明）** | §2：同一提交在 WSL2 原生 Linux 全绿；Windows 会话里 `git clone` 必失败 |
| 官方 Linux 环境（swebench 4.1.0） | **PASS** | §4：`import swebench` + `run_evaluation --help` 真实执行 rc=0 |
| Docker 资源 | **PASS** | §5：daemon 29.8.2 linux/amd64、镜像存储与磁盘实测充足 |
| 官方 Gold 控制 | **PASS（真实判分 resolved）** | §6：`marshmallow-code__marshmallow-1343` 官方报告 `resolved_instances=1` |
| 官方负向控制 | **PASS（4 种形态）** | §6：unresolved / patch apply failed / empty patch 全部按官方口径区分 |
| Adapter 真实输出解析与落盘 | **PASS** | §7：报告路径、字段、身份 sidecar 与真实官方字节一致 |
| 缓存与恢复 | **PASS（真实证据复用）** | §7.3：第二次同身份运行 `decision=REUSED`、换补丁被拒绝 |
| 完整 M2 预检查 | **BLOCKED（1 项）** | §8：WSL 内 GitHub 不可达，推理 workspace 无法在 Linux 侧建立 |
| 真实 GLM SWE-bench 单题实验 | **NOT RUN（按指令）** | §8：本轮禁止付费 API；准入条件见 §8.3 |

新发现并已修复：**P1×2**（§9）。真实运行还确认了 3 条"离线 mock 猜对/猜错"的官方事实（§4.3）。

## 1. 环境与产物绑定

| 项 | 值 |
|---|---|
| 宿主 | Windows 11 22631 x64，20 CPU，内存 8.2 GB（Docker daemon 视角） |
| 可信测试环境 | WSL2 `Ubuntu-24.04`（`Linux 6.18.40.1-microsoft-standard-WSL2`，x86_64，默认用户 root） |
| WSL 数据盘 | `D:\WSL\Ubuntu-24.04\ext4.vhdx`；容器内 `/` 1007 GB，实测空闲 954 GB |
| 宿主磁盘 | C: 空闲 25.6 GB；D: 空闲 235.9 GB（门禁与镜像都不落在 C:） |
| 仓库副本 | `/root/cp-run/CodePilot-Repair`（**ext4，不在 /mnt/c**），由原生 Linux `git clone` 自 `/mnt/c/.../CodePilot-Repair` |
| 环境安装 | `uv 0.13.1` + `uv sync --frozen --extra dev --extra faiss --python 3.12`（与 `.github/workflows/ci.yml` 同命令） |
| 未复制内容 | `.env`（CI 口径没有凭据文件；离线门禁不依赖它） |
| 需要的系统包 | `ripgrep`（apt，v14.1.0；CI 里同样 `apt-get install ripgrep`） |
| 官方 harness venv | `/root/swebench-venv`（`swebench==4.1.0`，与主 venv 隔离） |
| 证据目录 | `artifacts/phase11-wsl/`（Linux 门禁日志、官方 CLI 探针、真实控制产物） |
| 复现脚本 | `scripts/wsl-setup-testenv.sh`、`scripts/wsl-run-gate.sh`、`scripts/wsl-run-tests.sh`、`scripts/wsl-setup-swebench.sh`、`scripts/wsl-run-official-control.sh` |

日志与提交绑定：`gate-full.log` 头部写 `branch=`/`head=`；每轮门禁结束后把该文件另存为
`gate-full-<head8>.log`，避免下一轮 `exec > tee` 截断历史。

## 2. 297 项 pytest 失败的根因（任务 A）

### 2.1 Windows 会话侧的直接复现

同一台机器、同一个仓库，用项目自己的 venv 解释器发起子进程：

```
D:\...\python.exe 3.12.7   subprocess git --version  -> rc=0
subprocess git init/config/add/commit/log            -> rc=0
subprocess git clone <本地路径>                       -> rc=128
  sh (pid) D:\Program Files\Git\usr\bin\sh.exe: *** fatal error -
  NtCreateDirectoryObject(\BaseNamedObjects\msys-2.0S5-<hex>): 0xC0000022
```

失败点是 Git-for-Windows 的 MSYS 运行时：本地克隆走 `sh.exe -c git-upload-pack`，
而 `sh.exe` 在本会话的进程沙箱里无法在 `\BaseNamedObjects` 建立命名对象
（`0xC0000022` = ACCESS_DENIED）。因此**不是仓库权限问题，也不是 git 本身坏了**：
不经 `sh.exe` 的 git 子命令全部正常，凡是需要 local transport 的 `git clone` 必失败。

试过的规避（全部无效，仍是 rc=128）：`file:///` URL、`-c protocol.file.allow=always`、
`--bare`、`--local --no-hardlinks`、`GIT_SHELL_PATH=cmd.exe`、`GIT_UPLOAD_PACK=<exe>`。
**没有**尝试逃逸会话沙箱（不提权、不改组策略、不禁用安全功能、不用计划任务绕开子进程限制），
按任务书 A3 改为换环境验证。

补充差异：本会话 PATH 里 `rg` 存在（`shutil.which("rg")` 命中 Qoder 自带 ripgrep），
上一轮 Codex 会话没有 `rg` —— 那部分失败（≥13 项）属同一类"环境缺件"，不是代码回归。

### 2.2 归因证据：同一提交在 Linux 上全绿

代表性集合（任务书 A4 点名的模块）在两个环境的对照：

| 集合 | Windows（本会话） | WSL2 Ubuntu-24.04 |
|---|---|---|
| `test_workspace.py` + `test_gitcmd.py` + `test_index.py` + `test_synthetic.py` + `tests/graph` + `tests/sandbox/test_swebench_env.py` + `test_swebench_loader.py` | **50 failed / 139 passed / 3 skipped**（190.7s） | **448 passed / 14 skipped，exit 0**（139.8s） |

Windows 失败明细（`artifacts/phase11-acceptance-win/win-representative.log`）：
46 处 `NtCreateDirectoryObject`，异常类型为 `GitError` 46 例、`WorkspaceError` 44 例、
`AssertionError` 4 例；这 4 例断言的都是"错误信息里应包含某个具体原因"，而实际拿到的是
`git clone 失败: ... Could not read from remote repository.` —— 同一个克隆阻断的下游表现。

分类结论（按任务书 A4 的口径）：

| 类别 | 数量 | 依据 |
|---|---|---|
| ENVIRONMENT_BLOCKED | 297 / 297 | 同一提交在 Linux 全绿（§3）；Windows 侧失败签名集中于 msys `sh.exe` 与缺失 `rg` |
| REAL_REGRESSION | 0 | Phase 11 的 9 个测试文件在 Windows 也 0 失败；Linux 全量 0 失败 |
| TEST_FLAKINESS | 0（本轮未观察到） | Linux 全量一次通过，无顺序相关抖动 |
| TEST_ASSUMPTION_INVALID | 0 | 需要真实 `rg` 的用例在 CI 里也是"apt 装 ripgrep"，属环境前置条件而非测试假设错误 |
| UNKNOWN | 0 | — |

**注意**：这不是"旧文件里的失败自动算环境问题"。判据是同一份代码在第二个可信环境
（原生 Linux git + 原生 Python 子进程 + 真实 ripgrep）里跑到全绿，而不是文件年龄。

### 2.3 顺带发现的一个真实跨平台缺陷

Linux 门禁第一次跑出 **mypy 非 0**（Windows 上是 0）：

```
src/codepilot/sandbox/paths.py:35: error: "stat_result" has no attribute "st_file_attributes"
```

`st_file_attributes` 只存在于 Windows 的 `stat_result`，运行时靠
`if os.name == "nt"` 保护，但类型检查器按字面分析 → Linux（CI 的 runner）必红。
GitHub Actions 最后一次成功运行是 2026-10-11 的 Phase 2 合并（run `38155394051`），
Phase 3～11 从未 push，所以这个缺口一直没被暴露。修复见 `ed4b224`：改用
`getattr(entry.lstat(), "st_file_attributes", 0)`，行为不变、两个平台类型检查都过。
→ 归入 **REAL_REGRESSION 类的"门禁缺陷"**（门禁本身失败，但生产行为无回归）。

## 3. 全量离线质量门禁（Linux/WSL2）

命令与 CI 完全一致（`uv run ruff check .` / `ruff format --check .` / `mypy src` /
`uv run pytest -m "not docker and not llm" --cov --cov-report=term-missing`）。

最终一轮（HEAD `68e57b48dc6932dc098c7ed96d4520c70f2ffc02`，日志
`artifacts/phase11-wsl/gate-full-68e57b4d.log`，起于 2026-10-19T16:18:22+08:00）：

| 步骤 | exit | 结果 |
|---|---|---|
| `ruff check .` | 0 | All checks passed! |
| `ruff format --check .` | 0 | 375 files already formatted |
| `mypy src` | 0 | Success: no issues found in 168 source files |
| `pytest -m "not docker and not llm" --cov --cov-report=term-missing` | 0 | **3242 passed, 6 skipped, 45 deselected in 250.70s** |
| 覆盖率门禁（`fail_under=80`） | 0 | `Required test coverage of 80.0% reached. Total coverage: 90.60%` |

对照上一轮在 Windows 会话里的同命令结果（2927 passed / **297 failed** / 14 skipped /
45 deselected / 87.12% / exit 1）：**用例数一致口径、零裁剪、零新增 skip**，
失败项在同代码同命令的 Linux 环境里全部消失，覆盖率反而更高（Linux 上没有被沙箱
中断的 clone 路径拖累分支覆盖）。

第一轮 Linux 全量（修复前，HEAD `d9687c1`）：`3232 passed, 6 skipped, 45 deselected in 550.30s`，
`Total coverage: 90.59%`，mypy 1 错（§2.3）。这一轮已经证明：**297 项失败与生产代码无关**。

同环境 Windows 侧回归（改动后，真实执行）：

```
tests/eval/test_swebench_harness.py tests/sandbox/test_swebench_env.py
tests/eval/test_swebench_{loader,prediction,subsets,isolation}.py  -> 260 passed, 1 skipped
tests/eval/test_swebench_runner.py tests/integration/test_swebench_smoke.py
tests/eval/test_isolation.py tests/unit/sandbox/test_paths.py      -> 101 passed, 1 skipped, exit 0
```

未采用的"绿灯手段"：没有新增 `skip`、没有降低 `fail_under`、没有放宽断言、
没有把 `not docker and not llm` 改窄。唯一的 marker 变化是 `.gitignore` 增加
`pytest-of-*/`（本机会话 TMP 落在仓库内，ruff 会去 lint pytest 夹具副本）。

## 4. 官方 SWE-bench Linux Runtime（任务 B）

### 4.1 安装与可执行性

`/root/swebench-venv/bin/python -c "import swebench"` 成功；
`python -m swebench.harness.run_evaluation --help` **rc=0**，参数集（4.1.0 实测）：

```
-d/--dataset_name  -s/--split  -i/--instance_ids  -p/--predictions_path（必填）
--max_workers  --open_file_limit  -t/--timeout  --force_rebuild
--cache_level {none,base,env,instance}  --clean  -id/--run_id（必填）
-n/--namespace  --instance_image_tag  --env_image_tag  --rewrite_reports
--report_dir  --modal
```

`-d` 明确接受"数据集名**或本地 json/jsonl 路径**"，所以用本地快照固定 revision 是官方支持用法。

### 4.2 适配器命令 vs 官方 CLI

`scripts/swebench_official_probe.py` 用**生产代码**拼出 argv，然后把其中 `-p` 换成不存在的文件、
用真实官方 CLI 执行：结果 `accepted_by_argparse=true`，异常停在
`utils.py:65 get_predictions_from_file`，**在 `docker.from_env()` 之前**，且不碰 Docker。

```
artifacts/phase11-wsl/official-probe-dev.json          # 修复前：镜像名不匹配
artifacts/phase11-wsl/official-probe-dev-fixed.json    # 修复后：matches_official_default_namespace=true
```

### 4.3 三条"必须以真实字节为准"的官方事实

| 事实 | 4.1.0 真实行为（源码/实测） | 适配器的处理 |
|---|---|---|
| 汇总报告位置 | `Path(model.replace("/","__") + f".{run_id}.json")`，相对**子进程 cwd**；`--report_dir` 只被 `mkdir`（run_evaluation.py:505-508） | cwd 设为 `report_dir`，与真实产物路径逐次核对（§7.1） |
| 单实例证据 | `logs/run_evaluation/<run_id>/<model_slug>/<instance_id>/{report.json,run_instance.log,test_output.txt}`，同为相对 cwd | 同一 `cwd` 下解析；`patch_successfully_applied` 与补丁失败标记都从这里取 |
| 镜像名与 namespace | 默认 `-n swebench` → 远端预构建镜像 `swebench/sweb.eval.x86_64.<id 小写、`__`→`_1776_`>:latest`；`-n none` → 本地构建名；base/env 名**不带** namespace | 见 §9 P1-1（已修） |

冻结快照一致性（B4 第一项）：`scripts/check_env_specs_against_official.py` 在装有 4.1.0 的环境里
对比 `configs/swebench/env-specs-4.1.0.json` 与真实 `MAP_REPO_VERSION_TO_SPECS`：

```
repos_frozen=66  repos_official=66  only_in_frozen=[]  missing=[]  content_mismatches=[]  identical=true
snapshot_sha256=1cc0aa06cc220652db4a482ed8470b1fc17e59fad9a0df48b83602b1a04daf03
```

env 镜像名仍不是官方名：官方 22 位摘要来自 `str(env_script_list)`（`make_env_script_list` 要读
仓库里的 `requirements.txt`/`environment.yml`），离线不可复算。CodePilot 用冻结规格的候选键并
显式标注不等同官方 tag（`phase11.md` §4、`DEFERRED_CAPABILITIES`）。实测官方值示例：
`sweb.env.py.x86_64.2f217c8b4490bfa0e2ba14:latest`（marshmallow 2.20）。

## 5. Docker 资源预检（B2 / C4）

| 项 | 实测 |
|---|---|
| daemon | Docker Desktop Engine 29.8.2，`OSType=linux`，`/var/lib/docker`，containerd/overlayfs |
| CPU / 内存 | 20 vCPU / 8.2 GB |
| 镜像存储位置 | `D:\Docker\wsl\disk\docker_data.vhdx`（62.37 GB）→ 增长落在 **D:**，空闲 235.9 GB |
| 运行前 `docker system df` | Images 258 个 43.79 GB；Build Cache 105 条 19.73 GB；Volumes 24 个 6.825 GB；容器 18（15 运行） |
| 运行后 | Images 259 个 47.35 GB（**+1，仅本次 swebench 镜像**）；Build Cache 仍 19.73 GB；Volumes/容器数不变 |
| 未执行 | `docker system prune`、任何镜像/卷删除、容器重命名、Docker Desktop 设置改动、WSL integration 开关 |
| 访问通道 | WSL 内 `DOCKER_HOST=unix:///mnt/wsl/docker-desktop/shared-sockets/guest-services/docker.proxy.sock`（Docker Desktop 已预置的 guest socket；`docker.from_env()` 可直接用） |
| 网络 | WSL 本体：pypi 200、github 000、hub 000；**容器内**（走 Docker Desktop 代理 `http.docker.internal:3128`）：github/raw/pypi 均 200；registry 经 `docker pull` 实测可用 |
| 上一轮报告的修正 | "C 盘空闲 28.89 GB 可能不够"不成立：Docker 数据不在 C 盘，实际落 D 盘且空闲 236 GB |

镜像体积（只用 `docker manifest inspect` 查元数据，未下载）：

| dev 实例 | 压缩体积 | 层数 |
|---|---|---|
| pylint-dev__astroid-1978 | 933.8 MB | 16 |
| **marshmallow-code__marshmallow-1343**（选定） | 939.0 MB | 16 |
| marshmallow-code__marshmallow-1359 | 942.4 MB | 16 |
| sqlfluff__sqlfluff-1625 | 971.6 MB | 14 |
| pydicom__pydicom-901 | 1053.7 MB | 14 |
| pvlib__pvlib-python-1072 | 1625.5 MB | 16 |

选取依据：官方 dev split（23 条，revision `6ec7bb89…37cc2`）里挑纯 Python、依赖最轻、
FAIL_TO_PASS 只有 1 条的实例；`marshmallow-code__marshmallow-1343` 满足
"环境简单 + 资源可控 + 单次判分最短"。没有改动冻结的 test50/dev30。
本地落地体积 3.57 GB，image id `sha256:a61197e0b29461e34010cd83570424eb91a7f265574a81c82ff79e55c6c10f29`。

授权记录：2026-10-19 15:4x 用户批准"真实拉取 + 两次容器评测，用 `--cache_level instance`
保留镜像"，并要求门禁与 Docker 评测**串行**执行（不并发）。

## 6. 真实官方 Harness 控制（任务 C）

工具：`scripts/swebench_official_control.py`（+ `scripts/wsl-run-official-control.sh`）。
评测侧读取 `SWEBenchEvaluationTask.gold_patch`，不创建任何 LLM、不碰 InferenceTask；
oracle 预测写独立文件、独立 run_id、独立 report_dir。

| 控制 | 官方汇总（真实） | 适配器 verdict | decision | 用时 | run_id |
|---|---|---|---|---|---|
| **ORACLE_CONTROL**（gold patch，首次含镜像拉取） | `resolved_instances=1`，`resolved_ids=[marshmallow-code__marshmallow-1343]`，`error=0`，`empty=0` | `RESOLVED` | EXECUTED | 239.9 s | `cp-oracle-b6712c8b1352-oracle` |
| ORACLE_CONTROL 复跑（镜像已缓存） | 同上 | `RESOLVED` | EXECUTED | 50.5 s | 同上（新 report_dir） |
| **补丁能打上但没修好**（gold hunk 内注入失败语句） | `unresolved_instances=1`，`resolved=0` | `UNRESOLVED` | EXECUTED | 53.8 s | `cp-dea6e032d001-negative` |
| **补丁打不上**（非法上下文） | `error_instances=1`，`unresolved=0` | `PATCH_APPLY_FAILED`（修复前是 `ERROR`） | EXECUTED | 46.0 s | `cp-56a6f0f92b13-negative` |
| 同上，第二次运行 | 读已验证官方报告 | `PATCH_APPLY_FAILED` | **REUSED** | 0.0 s | 同上 |
| **空补丁** | `empty_patch_instances=1`，不启动评测容器 | `EMPTY_PATCH` | EXECUTED | 24.4 s | `cp-4f53cda18c2b-negative` |

官方单实例报告（gold）里可见隐藏测试口径：`FAIL_TO_PASS.success =
tests/test_marshalling.py::TestUnmarshaller::test_deserialize_wrong_nested_type_with_validates_method`，
`PASS_TO_PASS` 24 条全绿 → 判分链路（apply → 跑测试 → 按 F2P/P2P 归并）真实走通。

诚实边界：
1. `ORACLE_CONTROL` 只证明**评测器可用**，不是 Agent 成功，不进模型 Resolved Rate
   （`control_kind=ORACLE_CONTROL`、`included_in_resolved_rate=false`，两者都在产物 JSON 里）。
2. "打不上补丁"的负向控制同时打挂了 P2P（注入语句在模块导入路径上），它证明的是
   "打上 ≠ 解决"，不是"精细回归"。
3. 真实 GLM 推理、10/30/50/300 题批量：**NOT RUN**（本轮禁止）。
4. M3 仍未达成：固定 50 题的真实模型 + 官方 Harness 评测没跑。

产物：`artifacts/phase11-wsl/official-control*/`（官方汇总 JSON、身份 sidecar、
`logs/run_evaluation/**`、`_harness/{command,identity,report,result}.json`、stdout/stderr）。

## 7. 真实结果与 CodePilot 的对接（任务 D）

逐条核对（真实字节，不是 mock）：

| # | 要求 | 结果 | 证据 |
|---|---|---|---|
| 1 | 官方结果文件真实存在 | PASS | `report_path` 与 `expected_report` 字符串完全相同（§6 控制产物 JSON） |
| 2 | 实际路径与 Adapter 一致 | PASS | 同上；cwd=report_dir 的口径由 §4.3 源码事实支撑 |
| 3 | 原始官方字段解析准确 | PASS | `tests/fixtures/swebench_harness/real-gold-report-4.1.0.json` 由 `parse_report_file` 解析并通过断言（schema 2、计数、IDs） |
| 4 | `official_resolved` 只来自官方判分 | PASS | 只有官方 `resolved_ids` 给出 `RESOLVED`；`m2_verified` 不参与（`compare_with_internal_m2` 既有测试） |
| 5 | Gold 与模型结果分离 | PASS | 独立 predictions/run_id/model/report_dir；`included_in_resolved_rate=false` |
| 6 | 失败补丁不当成功 | PASS | wrong → `UNRESOLVED`/`resolved=false`；garbage → `PATCH_APPLY_FAILED`/`resolved=None` |
| 7 | 环境错误不归模型 | PASS（本轮加强） | 见 §9 P1-2：坏补丁从"泛化 ERROR"里被区分出来，真环境错误仍是 `INSTANCE_ERROR` |
| 8 | 损坏报告不进缓存 | PASS | 既有 `test_cache_corrupted_report_is_quarantined_and_rebuilt` + §7.3 真实复用 |
| 9 | 同实例不同补丁不误用 | PASS（真实字节） | 把真实 gold 报告放进同一 run_id，换补丁 → `CACHE_IDENTITY_MISMATCH` 且子进程 0 次调用 |
| 10 | 恢复只复用身份完整结果 | PASS | 真实 `REUSED`（0.0 s）；身份含 `namespace`，缺字段的旧 sidecar 解析即失败 |
| 11 | M2 内部验证与官方独立 | PASS | `official_authoritative=true`，discrepancy 单独成指标（既有测试 + §6 的 gold/模型分离） |
| 12 | 工作区与历史 Artifact 未污染 | PASS | `docker system df` 前后对比（§5）：只多 1 个 swebench 镜像、0 个残留容器；Windows 仓库 `artifacts/` 历史目录未改动 |

`PredictionWriter` / `ResultJournal` / `ArtifactStore` / Metrics / RunReport 本轮未改口径，
由 Linux 全量门禁（§3）与 Windows 定向回归共同覆盖；`classify_outcome` 新增一条真实
verdict → 归因的映射测试（`test_real_official_control_verdicts_map_to_the_expected_attribution`）。

## 8. SWE-bench 真实运行预检查（任务 E）

| 检查项 | 状态 | 证据 |
|---|---|---|
| 官方数据集可读取 | **READY** | loader 读本地快照，dev 23 / test 300；控制脚本真实取到任务对象 |
| Revision 匹配 | **READY** | `6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2`，写入 predictions/报告与身份 sidecar |
| InferenceTask 不含 Gold | **READY** | E-12/E-13 的字段与 import 隔离测试在 Linux 全量门禁里通过 |
| Base Commit 可 checkout | **BLOCKED（Linux 侧）** | WSL 内 `https://github.com` 不可达（curl 000）；容器内可达（200）。Windows 侧历史真实 clone 可用 |
| Docker 环境可准备 | **READY** | §5/§6：官方实例镜像真实拉取并判分；`docker.from_env()` 经 guest socket 可用 |
| 完整 M2 图可装配 | **READY（离线）/ NOT VERIFIED（带真实 workspace）** | `tests/graph` 在 Linux 全绿；端到端装配需要 §8 上一行的 clone |
| 模型配置可解析 | **READY** | Phase 10/GLM 配置链与 `llm-config` 文档；本轮未发起请求 |
| 单任务 / 整批费用上限存在 | **READY** | `SWEBenchRunConfig` 预算字段 + 既有测试（Linux 全量覆盖） |
| PredictionWriter 可用 | **READY** | §6 的 oracle/负向 predictions 均由生产写出路径落盘 |
| 官方 Harness Adapter 可用 | **READY** | §6：真实 Docker 判分 + §7 的解析/缓存核对 |
| Artifact 与 Checkpoint 路径独立 | **READY** | `artifacts/swebench/<run>/`（E-17 收口后默认 root）+ Phase 9 SQLite checkpoint |
| 结果归因与报告路径可写 | **READY** | §6 控制产物 + `_harness` 证据目录真实写入 |

### 8.1 唯一阻断：WSL 里没有 GitHub 出口

NAT 模式 WSL 不镜像宿主 `localhost` 代理（wsl.exe 每次都在提示这件事），而 GitHub
在直连条件下不可达。可选解法（**都需要你决定，本轮未动**）：

1. `/etc/wsl.conf` 开 `networkingMode=mirrored` + `wsl --shutdown`（改宿主网络栈，影响面大）；
2. 在 WSL 里显式指向宿主代理（FlClash 端口），只给需要的外部域名走代理；
3. 混合执行：推理阶段留在 Windows（GitHub 可用），官方评测用适配器已有的
   `use_wsl`（`wsl.exe -e ...`）通道进 Linux —— 需要验证 `/mnt/c` 路径与 report_dir
   约定，本轮未验证。

### 8.2 与"能不能开始真实跑批"有关的两条口径修正

* `--cache_level` 不显式传时官方默认 `env`，**跑完就删掉本次拉取/构建的实例镜像**；
  CodePilot 记录的是镜像身份，删掉后 preflight 必然报"镜像不存在"。本轮按你的授权用
  `instance` 保留（单实例 3.57 GB）。300 题全量前要先定这条策略（保留≈TB 级 vs 每题重下）。
* 官方 4.1.0 对"补丁打不上"抛 `EvaluationError`、只进 `error_ids`、**不写** `report.json`；
  已按 §9 P1-2 修复归因。

### 8.3 真实 GLM 单题实验准入判断

**BLOCKED（条件性）**。已满足：数据、revision、隔离、预算、Docker、官方判分链路、
Adapter 解析与缓存（全部有真实证据）。未满足：Linux 侧 workspace clone（§8.1）、
以及本轮明确禁止的付费 API 授权。执行一次真实单题（1 instance，GLM 推理 + 官方评测）
还需要你给出：① 上面的网络方案；② 真实 LLM 调用授权；③ 单题费用上限。

## 9. 本轮新发现的缺陷与修复

| 编号 | 级别 | 现象 | 根因 | 修复 |
|---|---|---|---|---|
| P1-1 | **P1** | preflight 记录的实例镜像名与官方实际使用的不是同一个；`--cache_level` 未显式传 | 官方 4.1.0 `-n` 默认 `swebench`（远端镜像，`__`→`_1776_`），适配器不传 `-n`，而 `instance_image_key()` 只算无 namespace 的本地构建名 | `cb01bcd`：`namespace`/`cache_level` 成为显式配置并进入 argv 与缓存身份；`instance_image_key` 实现官方命名规则（含 `_1776_`）；旧 sidecar 缺 `namespace` 即解析失败、拒绝复用 |
| P1-2 | **P1** | 模型给出打不上的补丁被记成泛化 `ERROR`/`INSTANCE_ERROR`，`PATCH_APPLY_FAILED` 在真实链路里永不出现 | 官方对补丁应用失败抛 `EvaluationError`、不写 `report.json`，只在 `run_instance.log` 留 `>>>>> Patch Apply Failed`；适配器只在"官方已判 unresolved"时读 report.json | `848a07d`：`ERROR` 且日志含官方标记 → `PATCH_APPLY_FAILED`；真实 garbage 控制前后对照（修复前 ERROR，修复后 PATCH_APPLY_FAILED，报告 sha 未变） |
| P2-a | P2 | Linux/CI 上 mypy 门禁失败（§2.3） | Windows-only `st_file_attributes` 被无条件类型引用 | `ed4b224` |
| P2-b | P2 | 官方 env 镜像名与 CodePilot 候选键不同 | 官方摘要来自 `env_script_list`（需联网读仓库文件） | **保留为已记录缺口**（`DEFERRED_CAPABILITIES`，`phase11.md` §4），不在本轮改口径 |
| P2-c | P2 | `--open_file_limit`/`--force_rebuild`/`--clean`/`--rewrite_reports` 未由适配器控制 | 4.1.0 存在但适配器未暴露 | 未改（默认值即官方推荐用法；`--clean` 保持 False 才不删别人的镜像）。记入 §10 |

## 10. 剩余阻断 / NOT RUN / DEFERRED

1. **真实 GLM SWE-bench 推理**：NOT RUN（本轮禁止付费 API）。
2. **固定 50 题（及 300 题）官方评测**：NOT RUN；M3 仍未达成。
3. **10 题真实费用校准**：NOT RUN，E-20 维持 `REAL_COST_CALIBRATION_PENDING`。
   本轮新增可用的**评测侧真实耗时**：镜像拉取+首次判分 239.9 s，热镜像复判 50.5 s，
   无效补丁 53.8 s，补丁打不上 46.0 s，空补丁 24.4 s（单实例、`max_workers=1`）。
4. **官方镜像作为推理执行环境复用**：DEFERRED（DA-018 不变）。
5. **WSL 内 GitHub 出口**：BLOCKED，方案见 §8.1。
6. **Linux GitHub Actions（DA-003）**：NOT RUN。本轮拿到的是**本机 WSL2 Linux**门禁证据，
   不等同于 CI；关闭仍需一次真实 push + workflow 运行链接（未获授权 push）。
7. `.playwright-mcp/` 既有未跟踪目录：未删除、未改动。

## 11. Git

| 提交 | 内容 |
|---|---|
| `ed4b224` | fix(sandbox)：非 Windows 也能类型检查 reparse 探针 |
| `cb01bcd` | fix(eval)：namespace / cache_level 与官方镜像身份一致（P1-1） |
| `f14781c` | chore(scripts)：WSL2 门禁 runner 与官方 CLI 一致性探针 |
| `3d19d12` | test(eval)：run_id 守卫改用同一 config 派生 namespace |
| `848a07d` | fix(eval)：读官方实例日志区分坏补丁与环境错误（P1-2） |
| `74ced3a` | test(eval)：真实官方产物入夹具 + 控制脚本 + `.gitignore` |
| `68e57b4` | chore(scripts)：冻结规格与已装 harness 的一致性核对 |
| 本提交 | 文档收口（本文件 + `phase11.md` / `phase11-review.md` / `05` / `experiment-plan`） |

远端：**未 push**；无 PR / merge / reset / rebase / amend / force push。
最终 `git status --short` 只剩既有未跟踪目录 `.playwright-mcp/`。
