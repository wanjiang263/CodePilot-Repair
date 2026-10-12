# Phase 4：Sandbox + Environment Building（S-01～S-23）

日期：2026-10-12 实现（S-01～S-17），2026-10-13 离线收口，2026-10-13 第二批真实 Docker 运行时验收（S-18～S-23）  
分支：`codex/phase4-sandbox-core`

## 设计摘要

执行链路是固定 base commit 的独立 Workspace → 严格 EnvSpec → 环境镜像 → 新测试容器 → JUnit XML → ExecutionResult → Artifact。环境构建与测试执行是两个不同容器：前者只有在显式授权（或项目自带受控 fixture）时允许网络，安装成功后提交为 `codepilot-env:<sha1>`；后者始终 `network_mode=none`，并在每次运行后强制删除。

缓存键由规范化的 `(repository, base_commit, EnvSpec)` 计算。镜像还保存 cache key、base commit 与 EnvSpec SHA-256 标签；标签缺失或不一致按缓存损坏处理并重建，构建失败不产生有效缓存。

`FakeSandbox` 只用于确定性的离线控制流测试。每个结果都包含 `execution_backend=fake` 与 `container_verified=false`，不能作为真实容器成功证据。Mock Docker SDK 测试覆盖参数、两阶段、缓存、超时、OOM 与定向清理；真实 Docker 结果单独报告。

## 安全限制

测试容器固定使用：

- `network_mode=none`；
- `user=1000:1000`；
- `read_only=true`，只挂载当前 Workspace 到 `/workspace`、当前 attempt 到 `/out`，`/tmp` 为 512 MiB tmpfs；
- 4 GiB 内存且无额外 swap、2 CPU、512 pids；
- drop ALL capabilities，`no-new-privileges`，`privileged=false`；
- 不挂载 Docker socket或其他宿主目录；
- Workspace 在 resolve 后检查允许根边界，并再次检查 symlink/reparse-point 逃逸；
- 超时后 kill，再 `remove(force=true)`；残留清理只查询 `codepilot=1` 且超过一小时的容器，不执行全局 prune；
- 完整日志先脱敏再写 Artifact，ExecutionResult 只保留可配置 UTF-8 tail。

这些措施是研发和评测场景的风险缓解，不是可对抗恶意代码的完整安全边界。

## 基础镜像
基础镜像只包含 Python slim、Git、CA 证书和最小构建工具，不包含用户仓库源码。可复现命令：

```bash
docker build --build-arg PYTHON_VERSION=3.12 -f sandbox/Dockerfile.base -t codepilot-sandbox-base:py3.12 .
```

用户 base commit 的源码只复制进该仓库专属的缓存环境镜像，测试时 `/workspace` 会被当前任务 Workspace 覆盖。

## Windows / Docker Desktop

Docker SDK 的 bind source 使用 Windows 原生绝对路径（例如 `C:\work\repo`），不会把 `/c/work/repo` 字符串替换当成 SDK 路径。Linux 容器内路径固定为 `/workspace` 与 `/out`。盘符、反斜杠、相对路径、Unicode、UNC 默认拒绝、`..` 和 symlink 逃逸均有单测。

## 收口轮次的回归点（2026-10-13）

最后一轮审查修复涉及的行为，逐条对上测试：

| 审查点 | 实现位置 | 覆盖测试 |
|---|---|---|
| stdout/stderr 与 JUnit 在写入 ArtifactStore 前脱敏；容器输出只暂存在 run 目录之外 | `sandbox/artifacts.py:persist_logs`、`sandbox/docker.py` 的暂存目录与 `redact_text(junit)` | `test_full_logs_are_redacted_and_tail_is_bounded`、`test_junit_artifact_is_redacted_before_entering_the_run_record`、`test_test_execution_is_offline_parses_junit_and_removes_container` |
| `.git/config`、`.env` 等不进环境镜像 | `sandbox/docker.py:_workspace_archive`、`.dockerignore` | `test_build_archive_excludes_credentials_and_git_remote_config` |
| timeout/OOM 原始状态不被后续 cleanup 失败覆盖 | `sandbox/docker.py`（`cleanup_error` 只在非超时时改写 `sandbox_error`）、`status.py` | `test_timeout_keeps_timeout_status_when_force_remove_reports_failure`、`test_oom_is_distinct_reason_without_seventh_status`、`test_cleanup_failure_on_normal_run_prevents_false_pass` |
| EnvSpec / SWE-bench 严格类型校验 | `sandbox/env_spec.py` | `test_env_spec_rejects_invalid_or_unsupported_content`、`test_swebench_malformed_commands_fail_loudly` |
| Docker 客户端初始化失败 → 结构化 SANDBOX_ERROR | `cli.py:run_tests` 的 `except DockerException` | `test_cli_docker_unavailable_is_structured_json` |
| 既有 CLI 命令保持兼容 | `cli.py`（`_PLANNED` 仅摘掉 run-tests） | `tests/unit/test_cli.py`（含 `test_unimplemented_commands_fail_loudly`） |

本轮补了两处：

1. `EnvSpec.timeout_seconds` 改为 strict int。YAML 1.1 把 `yes`/`no` 解析成布尔，宽松模式下 `timeout_seconds: true` 会静默变成 1 秒超时，把一个本来能通过的仓库判成 TIMEOUT；`from_swebench_spec` 也不再对 `timeout_seconds` 做 `int()` 截断（`1.9` 会被截成 1）。
2. 补 `test_junit_artifact_is_redacted_before_entering_the_run_record`：JUnit XML 落盘前脱敏此前只有实现、没有断言。

## 当前验收证据

最终离线门禁在 S-01～S-17 的最后一笔代码改动之后单独执行一次，四条命令全部 exit 0（原始日志与逐命令退出码在 `artifacts/phase4-sandbox/gate-20261013/`）：

| 命令 | 退出码 | 实测结果 |
|---|---|---|
| `ruff check .` | 0 | `All checks passed!` |
| `ruff format --check .` | 0 | `207 files already formatted` |
| `mypy src`（strict） | 0 | `Success: no issues found in 105 source files` |
| `pytest -m "not docker and not llm" --cov --cov-report=term-missing` | 0 | 收集 1854 项 → **1851 passed / 0 failed / 1 skipped / 2 deselected**，覆盖率 **94.43%**（门槛 80%），耗时 **779.14s（12:59）** |

- 唯一 skip：`tests/unit/retrieval/test_embeddings.py:104`，未设置 `CODEPILOT_TEST_EMBEDDING_MODEL`，与 Sandbox 无关。
- 2 项 deselected：`@pytest.mark.docker` 与 `@pytest.mark.llm` 各一项，按标记排除，不是通过。
- Sandbox 定向子集（`tests/unit/sandbox` + `tests/docker`）：**97 passed / 1 skipped**（skip 即真实 Docker smoke），exit 0，日志 `targeted-sandbox.log`。
- `codepilot.sandbox` 各模块行覆盖：`docker.py` 82%、`env_spec.py` 79%、`status.py` 93%、`junit.py` 91%、`paths.py` 87%、`baseline.py` 100%、`fake.py` 93%。覆盖率门槛按总量判定（94.43% ≥ 80%），未改动阈值、未弱化任何断言。

## 第二批：真实 Docker 运行时验收（S-18～S-23，2026-10-13）

用户手动启动 Docker Desktop 后，本批在真实 Linux 容器引擎上完成 S-18～S-23 全部六项验收，并跑通一次真实 `codepilot run-tests` 端到端执行。

### 运行环境（实测）

| 项目 | 实测值 |
|---|---|
| Docker Desktop | 4.94.0 (241994) |
| Engine / API | 29.8.2 / 1.56（最小 1.40） |
| context | `desktop-linux`，`OSType=linux` |
| 内核 | `6.18.40.1-microsoft-standard-WSL2`，cgroupfs |
| 容器运行时 | containerd v2.3.6、runc 1.5.1 |
| 宿主机可见资源 | MemTotal 8.22 GiB、NCPU 20 |
| Docker SDK for Python | 7.2.0（urllib3 2.8.0） |

### 测试与夹具设计

本轮新增 7 个真实 Docker 测试文件（`tests/docker/`，共 **24 个用例**）＋ 1 个共享夹具 `tests/docker/conftest.py`。每条断言的判据都来自真实 daemon 的 `inspect`/容器列表，或容器内部执行的输出；不使用 `FakeSandbox`、不使用 MockTransport。

两类刻意偏离，都只改参数不改生命周期，且都在测试文件 docstring 中写明：

1. **仅下调内存上限**（S-21/S-22 用 256 MiB）。生产契约仍是 4 GiB；内存是资源旋钮不是安全参数，下调只是为了不在 8 GiB 的开发机上白白占用 4 GiB。安全断言（断网、非 root、只读根、cap_drop、no-new-privileges）一律按生产参数验证。
2. **单次 `remove` 调用的故障注入**（用于走"清理失败仍保留原始证据"分支）。容器仍是真实创建、真实运行、真实 kill，只有那一次删除调用被替换成抛错；故障注入结束后按"已验证归属（`codepilot=1` + 本 run 标签）+ 明确容器 ID"定向回收，并在证据里记录该操作。

追踪机制：本轮每个容器都额外带 `codepilot.acceptance_run=<每个用例唯一的 id>` 标签，所以"本轮创建的资源"可以在 daemon 侧精确查询；后台观察线程（独立 SDK 连接，100 ms 轮询）记录容器确实进入过 `running`。残留清理的每次 daemon 查询都断言必须带 `codepilot=1` 过滤。两个会话级守卫（其他项目的容器、其他项目的镜像）全程看护，任何 `docker system prune` 式的全局销毁都会让守卫失败——本批结束时 `aegis-phase2-*` 两个容器与全部非 CodePilot 镜像仍在原处。

### S-18 超时（`tests/docker/test_timeout.py`，2 个用例）

| 判据 | 实测 |
|---|---|
| 配置 | `timeout_seconds=15`，死循环夹具 `test_never_returns` |
| 容器 | `899da6aafa0b…`（`nifty_lichterman`），观察状态序列 `created → running` |
| 结果 | `status=TIMEOUT`，`error_reason=EXECUTION_TIMEOUT`，`exit_code=null` |
| 耗时 | 15.367 s（≥ 配置值，未无限等待） |
| 回收 | 结束后按 run 标签查询残留 `[]`，`inspect_container` 报 `NotFound` |
| 未被污染 | `tests_passed=0/tests_failed=0`，`oom_killed=false`，daemon 侧 `NetworkMode=none`、`Privileged=false` |
| 清理异常分支 | 注入删除失败后仍为 `TIMEOUT/EXECUTION_TIMEOUT`，`diagnostics.cleanup_error` 记录原始失败；泄漏容器 `3548c9e971a9…` 核对 `codepilot=1` 后定向回收 |

**修复的实际缺陷（S-18-D1，P1）**：修复前这两个用例都返回 `SANDBOX_ERROR / DOCKER_OR_CONTAINER_ERROR`，不是 TIMEOUT。根因是 Docker Desktop 的 `npipe` 传输不把读超时报告成 `requests.exceptions.ReadTimeout`，而是包成 `requests.exceptions.ConnectionError`，真实超时链为 `ConnectionError → urllib3 ReadTimeoutError → TimeoutError`（实测打印异常链确认）。原实现只捕获 `(RequestsTimeout, TimeoutError)`，于是超时被外层 `except (OSError, …)`（`requests.RequestException` 继承 `IOError`）吞掉，`timed_out` 永远是 False。后果不只是状态标签错：`execution_route()` 会把 `SANDBOX_ERROR` 路由到 `finalize`，而 TIMEOUT 应当进 `diagnose`——Phase 5 会把"测试跑挂了"当成"Docker 坏了"，直接放弃修复。修复方式：新增 `_is_wait_timeout()`，沿 `__cause__/__context__` 链判定超时；非超时形态的 `ConnectionError` 原样 `raise`，真实 Docker 故障仍归 `SANDBOX_ERROR`。离线回归两条：`test_npipe_wrapped_read_timeout_is_still_a_timeout`、`test_connection_failure_without_timeout_stays_sandbox_error`。附带影响：超时分支此前不会执行 `container.kill()`，只靠 `finally` 里的 `remove(force=True)` 兜底；修复后走显式 kill 路径。

### S-19 网络隔离（`tests/docker/test_network.py`，3 个用例）

行为证据（容器内部真实执行，6 条探针 6/6 通过）：`/proc/net/dev` 只有 `lo`；`/proc/net/route` 没有默认路由；TCP 连 `8.8.8.8:53` 抛 `ENETUNREACH`；`getaddrinfo("pypi.org", 443)` 抛 `gaierror`；HTTPS 抓 PyPI 抛 `URLError`；同时 `import pytest` 成功——证明依赖是联网的环境构建阶段装进镜像的，测试阶段没有继承任何网络能力，只有产物。探针若"连上了"就用例失败，因此不存在假阳性；用 IP 直连 + 域名解析 + 高层 URL 三种路径，避免把 DNS 缓存或单一 API 异常当成隔离生效。

配置证据（daemon inspect 真实执行容器 `cbd4a11c6ce9…`）：`NetworkMode=none`、`Privileged=false`、`CapDrop=["ALL"]`、`SecurityOpt=["no-new-privileges"]`、`Mounts` 恰好 `/workspace` 与 `/out` 两个 bind，无 Docker Socket、无额外卷、`Binds` 里也没有。代码审计：`execution_container_config()` 纯函数输出同样不含 host 网络、privileged 或 socket 挂载。

### S-20 非 root 与容器限制（`tests/docker/test_user.py`，2 个用例）

容器内部 9 条断言 9/9 通过：`getuid/getgid == 1000`；`CapEff=0000000000000000`（drop ALL 生效）；`NoNewPrivs: 1`；`os.setuid(0)` 被拒；`/proc/mounts` 根挂载选项以 `ro` 开头且写 `/etc` 失败（`EROFS`）；`/tmp` 是 tmpfs 且可写；`/workspace` 可写；`/out` 可写且结果被正常收集。

daemon inspect（真实运行中的容器 `f4876af6ed03…`，`State=running`）逐字段对上 02 §13.3：`User=1000:1000`、`Memory=4294967296`、`MemorySwap=4294967296`（无额外 swap）、`NanoCpus=2000000000`、`PidsLimit=512`、`ReadonlyRootfs=true`、`Tmpfs={"/tmp":"size=512m"}`、`CapDrop=["ALL"]`、`SecurityOpt` 含 `no-new-privileges`、`Privileged=false`、`NetworkMode=none`、挂载只有 `/workspace`+`/out` 且 `RW=true`。全部断言按生产参数验证，未为了通过而放宽任何安全项。

这些限制是测试隔离措施。据此只宣称"用户仓库的测试跑不进宿主机网络、不以 root 运行、写不了根文件系统"，不宣称系统已具备对抗恶意代码的完整安全沙箱。

### S-21 OOM（`tests/docker/test_oom.py`，3 个用例）

daemon 侧真实证据（独立容器，256 MiB 上限）：`State.OOMKilled=true`、`wait StatusCode=137`、`State.ExitCode=137`、`State.Status=exited`。这台 daemon/内核确实会置位 `OOMKilled`，所以判定不依赖 137 这个退出码。

Sandbox 侧（同一夹具经 `DockerSandbox.run_tests` 真实执行）：`status=SANDBOX_ERROR`、`error_reason=OOM_KILLED`、`exit_code=137`、`diagnostics.oom_killed=true`、耗时 1.597 s、结束后残留 `[]`；并断言 `is_environment_problem=True` 且 `execution_route()== "finalize"`，即 OOM 映射到既有六态、不新造第七态、也不进诊断修复循环。清理异常分支：注入删除失败后 `OOM_KILLED` 与 `exit_code=137` 均保留，`cleanup_error` 记录在案，泄漏容器 `10444a2267a9…` 核对归属后定向回收。

内存上限只为触发受控 OOM 而下调到 256 MiB，测试前确认 daemon 正常、宿主机无资源压力；压力夹具写成 2 GiB 有界循环，即使上限失效也不会吃穿机器。

### S-22 容器清理与资源泄漏（`tests/docker/test_cleanup.py`，8 个用例）

五条分支各自真实创建 1 个容器、结束后残留为 `[]` 且 `inspect` 报 `NotFound`：

| 分支 | 结果 | 容器退出码 | 耗时 |
|---|---|---|---|
| PASS | `PASS` | 0 | 0.971 s |
| FAIL | `FAIL` | 1 | 0.998 s |
| TIMEOUT | `TIMEOUT` / `EXECUTION_TIMEOUT` | null | 5.366 s |
| OOM | `SANDBOX_ERROR` / `OOM_KILLED` | 137 | 1.108 s |
| 启动失败（无效网络，容器已创建但 `start` 失败） | `SANDBOX_ERROR` / `DOCKER_OR_CONTAINER_ERROR` | null | 0.321 s |

第六条分支（清理异常）：`cleanup()` 抛 `RuntimeError`，消息同时含容器 ID 与失败原因；注入失败期间容器仍在，故障开关关闭后按 ID 定向回收。

归属边界实测：一个 `codepilot=1` + `created_at=1` 的过期容器被回收；同镜像新建的"本轮刚创建"容器与一个不带 `codepilot` 标签的诱饵容器都完好无损。清理的每次 daemon 查询都断言 `filters == {"label": "codepilot=1"}`。全程未使用 `docker system prune`、未全局删容器、未清镜像或 Volume；会话级守卫确认其他项目的 2 个容器与全部非 CodePilot 镜像原地未动。

### S-23 环境镜像缓存（`tests/docker/test_env_cache.py`，5 个用例）

同一 `(repository, base_commit, EnvSpec)` 连续两次构建，全部为本机单次实测（每个用例用 pytest 唯一临时目录 ⇒ 仓库路径唯一 ⇒ 第一次必然是冷启动，不存在把缓存命中冒充冷启动）：

| 场景 | 缓存 Key | Image ID | 耗时 | `from_cache` | 新建容器数 |
|---|---|---|---|---|---|
| 冷构建 | `12c4e1ed5eb6e9f41845964ea96305bd6dfdb4ef` | `sha256:cc907b83a137…` | **7.976 s** | false | 2 |
| 第二次命中（新 Sandbox 实例） | 同上 | 同上 | **0.090 s** | true | **0** |
| 同实例连续第二次 | `2c0c168533bf06b942b658f63a6bdc4b29e396ab` | 一致 | 8.019 s → 0.060 s | false → true | 累计 1 |
| 删除镜像后重建 | `650b8683cff2cc1bc5f477c7415942678af8df95` | 重建为新 Image ID | **7.812 s** | false | 2 |
| 重建后再命中 | 同上 | 与重建一致 | **0.066 s** | true | 0 |

命中时**不重复执行依赖安装**的判据是行为级的：缓存命中那一次 `containers.create` 调用数为 0，而冷构建每次都真的建过构建容器；两次 Image ID 完全一致。实测时间差 7.976 s → 0.090 s（本机单样本，比值约 89 倍，只作为量级参考不外推）。

失效与身份：改 `install` 列表、改 base commit、换 repository 三种变体各自算出不同的 cache key，且在 `allow_network=False` 下都返回 `ENV_ERROR`（未命中）并带 `image_ref=None`——证明它们不会复用已缓存镜像。再把已缓存镜像重新打 tag 成"另一个仓库的 key"后构建，仍然是 `ENV_ERROR`：标签里的 `codepilot.cache_key` 与请求 key 不一致即拒绝复用，所以缓存机制不会错误复用其他仓库的环境。删除镜像后能正常重建并再次命中；两次真实测试执行都通过。

### 真实 CLI 端到端（`codepilot run-tests`）

文档规定的命令在本机不能原样执行，原因是既有的夹具事实，不是本轮改动：

```powershell
uv run codepilot run-tests --repo tests/fixtures/repos/mini_auth --tests "tests/test_auth.py::test_login"
```

返回 `SANDBOX_ERROR / WORKSPACE_OR_ENVSPEC_ERROR`，detail 明确：`tests/fixtures/repos/mini_auth` 是 Phase 1 的**解析对象夹具**，目录里没有 `.git`，而本系统需要 git 历史来固定 base commit；该夹具里也不存在 `tests/test_auth.py::test_login`（实际测试文件是 `tests/test_tokens.py`、`tests/test_session.py`）。这是结构化失败而非缺陷，因此按"使用仓库实际有效配置"的要求，改用同结构受控 git 夹具执行端到端验收：`artifacts/docker-accept/e2e/e2e-fixture`（真实 git 仓库，含 `env.yaml`、`auth.py`、`tests/test_auth.py::test_login`，三个提交分别是全绿 / 反转密码校验 / 删除 `login` 符号），节点 ID 与文档一致。测试材料全部是仓库自带的受控 fixture，没有执行任何未知来源的安装脚本。

该夹具建在 gitignore 的 `artifacts/` 下，不入库；为保持可复现，定义如下（四个提交依次对应全绿、反转密码校验、删除 `login` 符号、恢复并加一个会把 token 打进 stdout/stderr/JUnit 的失败用例）：

```text
env.yaml            python_version "3.12" / install: [python -m pip install pytest]
                    test_command: python -m pytest -p no:cacheprovider --junitxml=/out/junit.xml
                    timeout_seconds: 120
conftest.py         把仓库根加入 sys.path
auth.py             USERS = {"demo": "hunter2"}; login(user, password) -> bool
tests/test_auth.py  test_login()                     → 通过
                    test_login_rejects_wrong_password()
tests/test_leak.py  失败用例，把 sk-acceptance123456789 打进 stdout 与 JUnit 失败文本
```

验收矩阵（全部真实容器执行，`execution_backend=docker`、`container_verified=true`、`network_mode=none`）：
| 用例 | CLI 退出码 | `status` | 容器退出码 | tests | passed/failed | 缓存 |
|---|---|---|---|---|---|---|
| FAIL 分支（反转密码校验的提交） | 1 | `FAIL` | 1 | 1 | 0/1 | 冷 |
| PASS 分支（全绿提交，首次） | 0 | `PASS` | 0 | 1 | 1/0 | 冷 |
| PASS 分支（同身份，**不加** `--allow-build-network`） | 0 | `PASS` | 0 | 1 | 1/0 | **命中** |
| 全量选择（省略 `--tests`） | 0 | `PASS` | 0 | 2 | 2/0 | 命中 |
| 泄漏脱敏提交全量 | 1 | `FAIL` | 1 | 4 | 2/2 | 冷 |
| 删除被测符号的提交 | 1 | `COLLECTION_ERROR` | 4 | 1 | 0/1 | 冷 |

链路逐段核对：真实 git fixture → Workspace/base commit（`--base-commit` 固定到指定 SHA，缓存按身份分键）→ `env.yaml` EnvSpec → 环境镜像 → DockerSandbox → 容器内隔离执行 pytest → `/out/junit.xml` → ExecutionResult JSON → ArtifactStore（`junit.xml` + `stdout.log` + `stderr.log` 三份文件实测存在且可读）→ 容器回收（每次运行后本 run 标签查询残留为 `[]`）。测试执行确实发生在 Linux 容器内：容器里是 `platform linux / Python 3.12.15`，宿主机是 Windows。JUnit 与 ExecutionResult 数量一致：4 个 `testcase` 对应 4 条 `tests`，失败数 2 对 2。原始夹具仓库未被污染：运行前后 `git rev-parse HEAD`、`git status --porcelain`、文件清单逐项相同。

脱敏实测：夹具里一个**失败**用例把 `sk-acceptance123456789` 同时打进 stdout、stderr 和 JUnit 失败文本。修复后 `junit.xml`、`stdout.log`、`stderr.log` 与 CLI 输出的 JSON 四者都不含原文，且 `***REDACTED***` 出现在 junit/stdout/JSON 中。注意一个容易自检失败的坑：pytest 默认捕获 stdout，**通过**用例的 `print` 不会出现在输出里，所以探针必须让用例失败。

### 本轮修复的缺陷汇总

| ID | 级别 | 症状 | 根因 | 修复 | 回归测试 |
|---|---|---|---|---|---|
| S-18-D1 | P1 | 真实超时被判成 `SANDBOX_ERROR/DOCKER_OR_CONTAINER_ERROR`，Phase 5 路由走 `finalize` 而非 `diagnose` | Docker Desktop npipe 把读超时包成 `requests.exceptions.ConnectionError`，只捕获 `Timeout` 形态漏判 | `sandbox/docker.py:_is_wait_timeout()` 沿 `__cause__/__context__` 链判定 | 离线 2 条 + `tests/docker/test_timeout.py` |
| S-23-D2 | P1 | CLI 输出的 `ExecutionResult` JSON 原样带出容器产生的 `sk-` token | 落盘三份 artifact 都脱敏了，但 `ExecutionResult.tests` 用的是内存里的 JUnit 解析结果，`message/traceback` 未过 `redact_text` | `sandbox/docker.py:_redacted_case()`，在沙箱边界统一脱敏；状态/计数/node_id 不变 | `test_junit_failure_text_is_redacted_inside_execution_result` + CLI 端到端实测 |
| WS-D3 | P1 | `codepilot run-tests` 在任何机器上都建不出 workspace，返回 `WORKSPACE_OR_ENVSPEC_ERROR` | `cache_dir` 默认值是相对路径 `cache`；`_fetch_base` 把缓存目录当 git 远端传给 `git fetch <cache>`，而该命令的 cwd 是新建的 workspace，相对路径在那里解析不到 | `repo/workspace.py:WorkspaceManager.__init__` 把 `cache_root` 规范化为绝对路径（不解析 symlink，纯附加） | `test_relative_cache_root_still_creates_a_workspace` |

三个缺陷都不是"降低安全配置/放宽断言/删测试"换来的 PASS：S-18-D1 与 WS-D3 是补齐原本就该对的行为，S-23-D2 是让脱敏覆盖它声称覆盖的第三条路径。

### 观察项（不阻断，未修改）

- `EnvSpec.canonical_json()` 把 `timeout_seconds` 也算进镜像身份，因此只调超时会让整份依赖缓存失效并重装依赖。它是 EnvSpec 字段，S-23 的"改 EnvSpec 后身份变化"反而依赖这一点。属于可优化的缓存粒度，不是正确性问题，留待后续设计决定。
- `parse_junit` 用 `xml.etree` 解析由被测仓库在容器内产出的 XML。理论上仓库可以构造实体膨胀/外部实体输入；但同一个仓库本来就能在容器里执行任意代码，且 02 §13 已声明这不是对抗恶意代码的边界。本轮不改冻结的 JUnit 契约，只记录。
- 只读根 + bind 挂载下 `/out` 的宿主目录权限由 Docker Desktop 代为映射，因此 `output_dir.chmod(0o777)` 在 Windows 宿主上是无效但无害的操作。

### 第二批门禁结果

真实 Docker 验收与离线门禁分开报告。Docker 侧：

| 命令 | 退出码 | 实测 |
|---|---|---|
| `pytest tests/docker -m docker -v` | 0 | **24 passed / 0 failed / 0 skipped**，121.13 s（2:01） |

离线侧（本轮改了 `src/codepilot/sandbox/docker.py` 与 `src/codepilot/repo/workspace.py` 两处生产代码，因此完整重跑一次）：

| 命令 | 退出码 | 实测结果 |
|---|---|---|
| `ruff check .` | 0 | `All checks passed!` |
| `ruff format --check .` | 0 | `214 files already formatted` |
| `mypy src`（strict） | 0 | `Success: no issues found in 105 source files` |
| `pytest -m "not docker and not llm" --cov --cov-report=term-missing` | 0 | 收集 1881 项 → **1855 passed / 0 failed / 1 skipped / 25 deselected**，覆盖率 **94.46%**（门槛 80%），耗时 **701.09 s（11:41）** |

- 与第一批的 1851 passed 相比多出的 4 条，全部是本轮三个缺陷的**离线**回归测试：`test_npipe_wrapped_read_timeout_is_still_a_timeout`、`test_connection_failure_without_timeout_stays_sandbox_error`、`test_junit_failure_text_is_redacted_inside_execution_result`、`test_relative_cache_root_still_creates_a_workspace`。
- 25 项 deselected = 24 条 `@pytest.mark.docker`（第二批新增 23 条 + 原有 smoke 1 条）+ 1 条 `@pytest.mark.llm`；它们按标记排除，**不计作离线通过**，其真实结果由上一节的 Docker 门禁单独给出。
- 唯一 skip 仍是 `tests/unit/retrieval/test_embeddings.py:104`（未设置预安装 Embedding 模型），与 Sandbox 无关。
- Sandbox 模块行覆盖（本轮离线口径）：`docker.py` 83%、`env_spec.py` 79%、`base.py` 96%、`status.py`/`junit.py` 91–93%、`paths.py` 87%、`baseline.py` 100%。阈值未改、断言未弱化。
- 最终代码一致性：上表四条命令与 Docker 门禁都在**最后一笔代码改动之后**执行；文档改动（本节）不再触及 `src/` 与 `tests/`，因此门禁结果对最终提交有效。

运行时证据（逐条容器 ID、耗时、daemon inspect 字段）：`artifacts/docker-accept/runtime-evidence.jsonl`；CLI 端到端证据：`artifacts/docker-accept/e2e/`（`cli-matrix.json`、`final-report.json`、`leak-after-fix.json`）与门禁日志 `artifacts/phase4-sandbox/gate-runtime-20261013/`。这些目录都在 `.gitignore` 内，只作为本机可追溯证据，不入库。

## 第一批记录：Docker 运行时状态（历史，已被第二批取代）

- Docker CLI 29.8.2 可用；`docker info` **exit 1**，实际错误：`failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine; check if the path is correct and if the daemon is running: open //./pipe/dockerDesktopLinuxEngine: The system cannot find the file specified.`（原始输出 `docker-info.log`）。
- 因此真实 Docker Smoke、网络隔离、UID、OOM、残留清理、第二次缓存命中耗时全部 **NOT RUN**。本轮只检查一次 daemon 状态，未尝试启动/重装 Docker，也未触碰其他项目的容器资源。
- 因 daemon 不可用，只能确认安全参数配置与 Mock 生命周期行为；不宣称运行时网络/UID/OOM/残留隔离已经验收。
- 早前一份记录里的 `88 passed` 是本轮之前的中间数，已被上面的实测数字取代；此前被人工 Ctrl+C 中断的那一次全量运行既不作为 PASS，也不作为测试失败。

## 延后项

第二批已关闭 **DA-007**：真实 Docker daemon 可用，S-18～S-23 六项运行时验收与真实 `codepilot run-tests` 端到端全部实测通过（见上文），无未修复的 P0/P1 Sandbox 缺陷。

DA-001～DA-006 保持原状态，不因本轮 Docker 测试自动关闭：DA-001 Token 估算真实模型校准、DA-002 Symlink Linux CI 验证、DA-003 Phase 3 Linux CI、DA-004 L-18 真实 API Smoke、DA-005 C-14 真实模型定位评估、DA-006 真实 Issue Benchmark。L-18、C-14、Linux CI 与真实问题 Benchmark 本轮只维护延期记录，未调用真实 LLM 或付费 API。

台账位置：`docs/05-精细化待办清单.md` §23.2。

## Phase 4 当前状态（第二批收口后）

S-01～S-17 = IMPLEMENTED + 离线验收通过；S-18～S-23 = **PASS（真实 Docker 运行时实测）**；真实 `codepilot run-tests` 端到端 = **PASS**；最终离线门禁与 Docker 门禁全部 exit 0；DA-007 = **已通过并关闭**。因此 **Phase 4 正式验收完成**，具备进入 Phase 5（Planning + Coding + Patch Validation）的工程条件。

Sandbox 对 Phase 5 的就绪判据（本轮实测支撑）：`Sandbox` Protocol 的真实实现能固定 base commit、按 EnvSpec 复用镜像缓存、在断网/非 root/只读根容器里执行 pytest、产出与 JUnit 一致的六态 `ExecutionResult`、把失败文本与日志脱敏后再进运行记录，且每次运行后不残留容器。TIMEOUT 与 OOM 在状态与路由两个层面都可区分，这正是修复循环判断"是否进入诊断"的前提。

仍然成立的边界声明：以上限制是研发/评测场景的风险缓解，不是可对抗恶意代码的完整生产级安全边界（02 §13、本文《安全限制》节）。Phase 5 不得把这套隔离当作安全承诺来写文档或做验收。

本轮未提前实现任何 Phase 5 功能，未触碰 Phase 2 Retrieval 与 Phase 3 Localization 的冻结契约。

## Phase 4 整体状态

S-01～S-17 = IMPLEMENTED（离线实现 + 无 daemon 测试）；最终离线门禁 = 见上节实测数字；真实 Docker Runtime 验收 = **NOT RUN**；S-18～S-23 = 未完成；因此 **Phase 4 正式验收尚未完成**。

> 本节是第一批（S-01～S-17）的收口状态，保留作为历史记录。真实 Docker 运行时验收已在第二批完成，见上文《第二批：真实 Docker 运行时验收》；当前整体状态以下文《Phase 4 当前状态》为准。
