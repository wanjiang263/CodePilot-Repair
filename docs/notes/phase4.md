# Phase 4：Sandbox + Environment Building（S-01～S-17）

日期：2026-10-12 实现，2026-10-13 离线收口  
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

## Docker 运行时状态

- Docker CLI 29.8.2 可用；`docker info` **exit 1**，实际错误：`failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine; check if the path is correct and if the daemon is running: open //./pipe/dockerDesktopLinuxEngine: The system cannot find the file specified.`（原始输出 `docker-info.log`）。
- 因此真实 Docker Smoke、网络隔离、UID、OOM、残留清理、第二次缓存命中耗时全部 **NOT RUN**。本轮只检查一次 daemon 状态，未尝试启动/重装 Docker，也未触碰其他项目的容器资源。
- 因 daemon 不可用，只能确认安全参数配置与 Mock 生命周期行为；不宣称运行时网络/UID/OOM/残留隔离已经验收。
- 早前一份记录里的 `88 passed` 是本轮之前的中间数，已被上面的实测数字取代；此前被人工 Ctrl+C 中断的那一次全量运行既不作为 PASS，也不作为测试失败。

## 延后项

S-18～S-23 的正式运行时验收（死循环、网络、非 root、OOM、残留、真实缓存性能）保留到下一批，登记为台账 **DA-007**（见 `docs/05-精细化待办清单.md` §23.2）。L-18、C-14、Linux CI 与真实问题 Benchmark 继续 NOT RUN/延后，不在本轮调用真实 LLM 或付费 API。

Deferred Acceptance 台账当前七项，本批次只新增 DA-007、未改动其余六项状态：DA-001 Token 估算真实模型校准、DA-002 Symlink Linux CI 验证、DA-003 Phase 3 Linux CI、DA-004 L-18 真实 API Smoke、DA-005 C-14 真实模型定位评估、DA-006 真实 Issue Benchmark、DA-007 Phase 4 真实 Docker 运行时验收。

## Phase 4 整体状态

S-01～S-17 = IMPLEMENTED（离线实现 + 无 daemon 测试）；最终离线门禁 = 见上节实测数字；真实 Docker Runtime 验收 = **NOT RUN**；S-18～S-23 = 未完成；因此 **Phase 4 正式验收尚未完成**。
