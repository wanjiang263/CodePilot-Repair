# Phase 16：真实推理 + Live M2 E2E 一次性执行

日期：2026-10-22。状态：**真实推理成功；Live M2 全链路已执行，但官方与内部验证均被同一官方镜像环境问题阻断，官方判分记为环境失效，不重跑。**

本记录只写本轮实际发生的调用、费用与判定。没有全量 pytest、test50/test300、批量付费评测；
没有 commit、push 或 PR。历史 artifact、设置数据库与凭据均未覆盖或改写。

---

## 1. Docker 阻断根因与修复证据

### 1.1 Phase 15 留下的失败现场

`artifacts/phase15/provider-preflight.json`（2026-10-21T16:28:30Z，本地 2026-10-22 00:28）
记录了同一时刻的两条失败：

- Docker SDK：`DockerException: Error while fetching server API version: (5, 'CreateFile', '拒绝访问')`
- Docker CLI：`permission denied while trying to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine`

说明阻断不在项目代码里：Python SDK 与 `docker.exe` 同时被命名管道拒绝，而引擎本身没有报
「daemon 不存在」。

### 1.2 本轮复核结果

| 检查 | 实际结果 | 证据 |
|---|---|---|
| Docker CLI | `docker version` 成功，Server 29.8.2，API 1.56，Linux engine | 命令输出 |
| Docker SDK（当前 venv） | `docker.from_env().ping()` = `True` | `artifacts/phase16/provider-preflight-dockercheck.json`：`available=true`、`via=sdk`、`server_version=29.8.2` |
| 显式 npipe | `npipe:////./pipe/dockerDesktopLinuxEngine` 与 `npipe:////./pipe/docker_engine` 均 ping 成功 | 显式 `DockerClient(base_url=...)` 探测 |
| 管道 ACL | Owner `LAPTOP-H0FUOTNB\lenovo`；`lenovo` 与 `docker-users` 均有 Read/Write/Synchronize；SYSTEM/Administrators FullControl | `artifacts/phase16/check_pipe_acl.ps1` 输出 |
| WSL guest socket | `DOCKER_HOST=unix:///mnt/wsl/docker-desktop/shared-sockets/guest-services/docker.proxy.sock` 可用 | 真实拉取镜像、真实容器执行、官方 Harness 全链路 |
| 真实容器 | 从官方 pvlib 实例镜像启动 `--pull=never --network none` 容器并执行命令 | `artifacts/phase16/live-evidence/base-control.log` |

### 1.3 结论与边界

- 阻断的实际来源是 **Docker Desktop 侧的命名管道会话/ACL 状态**，不是仓库代码。当前 ACL
  对 `lenovo` 与 `docker-users` 均为读写；SDK、CLI、WSL guest socket 三条通道都已实测可用。
- **本轮没有重启 Docker Desktop**，也没有执行任何 `prune`、`rmi`、卷删除或设置变更。重启会
  影响当时正在运行的 19 个其他项目容器，而现有通道已经可用，按最小影响原则不重启。
- 旧失败是瞬时的引擎/管道状态；当前无法用同一条命令复现拒绝访问。交付口径是
  「已恢复并完成真实执行」，不是「改代码修复」；仓库没有为此改动生产代码。
- Docker Desktop 的 VM 日志显示 `init.log` 在 00:03 有过一次引擎初始化轮转，当前
  `com.docker.backend` 自 09:47 起持续运行；没有证据表明是项目配置或凭据导致。
- 容器/卷清理见 §7；22 个既有容器与 26 个卷在运行前后完全一致。

---

## 2. 真实最小推理（BigModel `glm-5.3-flash`）

### 2.1 正式设置与凭据引用

实际 Settings 库 `artifacts/settings/settings.sqlite3` 初始为 **0 个 Provider**。本轮通过
`SettingsService.create_provider()` 创建了唯一 Provider：

| 字段 | 值 |
|---|---|
| provider_id | `bigmodel` |
| protocol | `openai_compatible` |
| base_url | `https://open.bigmodel.cn/api/paas/v4` |
| credential | `env` → `GLM_API_KEY`（只存变量名，不存值） |
| model | `glm-5.3-flash` |
| context / max output | 1,000,000 / 4,096 |
| price | CNY 0.8 / 2.8 / 0.23 每百万 input/output/cached |
| 设置库版本 | 0 → 1 |

变更前数据库已备份为 `artifacts/phase16/settings-before-inference.sqlite3`。连接测试记录写入
`settings_connection_tests`：`kind=inference`、`ok=1`、`model_id=glm-5.3-flash`，
`checked_at=2026-10-21T18:43:16.726986+00:00`，fingerprint
`792a3fe366abdc83da3bdb668d8b565e4e5486089964a36df7e09bdc79e3e914`。

### 2.2 请求与预算

- 入口：项目正式 `SettingsService.test_connection(kind="inference", confirm_cost=True)`，
  内部构造真实 `OpenAICompatibleProvider` 并发出 HTTP 请求；没有 MockTransport、FakeLLM
  或缓存响应。
- 请求：固定短提示，`max_output_tokens=16`，`temperature=0.0`，`purpose=connection_test`，
  `max_attempts=1`（不重试）。
- 预派发费用上界：按 1,024 input + 16 output 的保守上界为
  **CNY 0.000864**，低于硬上限 **CNY 0.50**。
- 凭据只在进程内解析，证据文件不包含 Key 值；扫描结果见 §7。

### 2.3 实际响应

| 项 | 结果 |
|---|---|
| HTTP / 认证 | 成功，`ok=true`，`authenticated=true` |
| 配置模型 / 请求模型 | `glm-5.3-flash` |
| 响应 `model_echo` | `__connection_test__`（Settings 内部别名，不是供应商原始 model 字段） |
| finish_reason | `length`（16 token 上限截断） |
| input / output / cached | 30 / 16 / 0 tokens |
| 费用 | **CNY 0.0000688**（`cost_known=true`） |
| 延迟 | 2,359 ms |
| 账本 | `artifacts/phase16/cost-ledger.json`：`status=settled` |

**诚实边界**：`_test_inference()` 只持久化 `model_echo`（内部别名），没有保存供应商原始
`model` 字段；因此本轮能证明「请求以 `glm-5.3-flash` 发出并被接受、用量与费用可结算」，
不能声称已从响应体回读供应商的原始模型字符串。Live M2 的派发账本同样以冻结的
`spec.model=glm-5.3-flash` 记账；`CallRecorder` 按设计不落 `raw`。

证据：`artifacts/phase16/minimal-inference-2026-10-21T184314293685+0000.json`。

---

## 3. 真实 Live M2 E2E（`pvlib__pvlib-python-1072`）

### 3.1 运行身份与冻结条件

| 项 | 值 |
|---|---|
| instance / split | `pvlib__pvlib-python-1072` / `dev` |
| base commit | `04a523fafbd61bc2e49420963b84ed8e2bd1b3cf` |
| repo | `https://github.com/pvlib/pvlib-python` |
| dataset | `princeton-nlp/SWE-bench_Lite`，revision `6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2` |
| model alias | `glm-5.3-flash-main` → 实际 `glm-5.3-flash` |
| provider | `bigmodel`，OpenAI 兼容，API key 引用 `GLM_API_KEY` |
| run label / run_id | `phase16-l1-20261022` / `swebench-smoke-phase16-l1-20261022-pvlib__pvlib-python-1072-s0-52c2e7b7` |
| artifact root | `artifacts/phase16/live/`（全新；未覆盖 Phase 11–15 产物） |
| Docker | Docker Desktop Engine 29.8.2，Linux engine；WSL guest socket |
| 官方镜像 | `swebench/sweb.eval.x86_64.pvlib_1776_pvlib-python-1072:latest`，id `sha256:c8e43bebd10d7d2330820af520361dc6adc9642f98c7b7b7c415cca39852ffdd`，5,917,247,577 bytes |
| 官方 Harness | `swebench==4.1.0`，`/root/swebench-venv/bin/python` |
| 图预算 | `max_cost=3.00 CNY`、`max_runtime=1800s`、`max_test_runs=12`、`max_iterations=5` |
| 派发账本 | `CODEPILOT_DISPATCH_BUDGET_CNY=3.00`，新账本路径 `/root/cp-run/phase16/dispatch-ledger.json` |

工作区来自官方实例镜像内的 base commit，先落成单提交 shallow bare mirror，再经
`WorkspaceManager` 克隆；`_assert_isolated` 的「恰好 1 个可达提交」检查通过。证据：
`artifacts/phase16/live-evidence/source-identity.json`。

### 3.2 实际节点序列

图级节点（`events.jsonl` 中 `node_start/node_end/stop`）：

```text
understand_issue  ok
retrieve          ok
localize          ok
analyze_impact    ok
build_env         ok（真实官方镜像；1 次基线测试尝试）
reproduce         failed（复现器生成测试 4 次，均 collection_error）
plan              structured_attempt 1/2/3 → ok
generate_patch    finish
validate_patch    passed（6 项检查；3 文件；+7/−7；risk=high）
run_target_tests  failed:tests_failed
finalize          failed；stop_reason=tests_failed
```

内部报告同时记录：

- `status=failed`，`verification_level=failed`，`stop_reason=tests_failed`
- `iterations=1`，`attempts_saved=1`，`test_runs=9`，图运行 419.63 s
- 未执行阶段：`target_tests`、`regression`、`review:the reviewer produced no result`
- `review_calls=0`，`regression=null`，`review=null`
- `failure_reasons=[「no authorized target test available: the patch was never executed,
  so repair cannot be self-assessed」]`
- 闭环准入：`feasible=true`，`runs_spent=5`，`allowance=12`，`required=1`，
  `coder_share=4`；预算准入本身没有拒绝，失败发生在目标测试不可用时。

### 3.3 模型调用与 Token

| 角色 | 调用 | input | output | cached | CNY |
|---|---:|---:|---:|---:|---:|
| localizer | 2 | 38,097 | 376 | 0 | 0.0315304 |
| reproducer | 8 | 52,820 | 4,047 | 0 | 0.0535876 |
| planner | 4 | 51,861 | 2,476 | 0 | 0.0484216 |
| coder | 18 | 212,185 | 1,878 | 181,504 | 0.07154912 |
| **合计** | **32** | **354,963** | **8,777** | **181,504** | **0.20508872** |

`unknown_cost_calls=0`，费用不是下界。派发账本 32 条全部 `settled`，
`accounted_or_reserved=0.2050887200000001`，`frozen=false`，与 `usage.json` 完全一致。
没有 Reviewer 调用，因此没有把审查费用算进本轮。

### 3.4 补丁与内部验证

- 最终补丁：3,736 bytes，SHA-256
  `ab393289bf1dc1da0a5c0d7b7f0153d656081d581898ba755b363c30068643a7`
- 候选补丁 SHA-256：`5da21b95285210106374a966c756274a2da3a245738d2bede382ccf8f947d8cc`
- 预测 SHA-256：`35b4a6fe82835e834ad176d501993f4a79b488854d93b0d5b5fa6f0e4f905dfd`
- 补丁内容：`pvlib/temperature.py` 的 `fuentes()` 改为
  `np.diff(poa_global.index.values.astype(np.int64))`；同时把 `pvlib/pvsystem.py`、
  `pvlib/singlediode.py` 中 5 处 `np.Inf` 改为 `np.inf`。后者与本题根因无关，是被
  patch validator 标为 `risk=high`、`signature changed` 的额外改动。
- PatchValidator：6 项全部通过（path/size/non_empty/test_protection/syntax/cheat）。
- Coder 的 4 次 `run_tests` 都以 `COLLECTION_ERROR`（exit 4）结束；复现器 4 次尝试也都是
  `collection_error`。目标测试节点因此没有可授权的测试可执行，回归与审查没有启动。
- 计划阶段曾试图把新测试写进受保护的 `pvlib/tests/test_temperature.py`，被
  `existing_test_is_protected` 正确拒绝；本轮没有改写既有测试。

### 3.5 官方 SWE-bench 判分

官方命令真实执行，`decision=EXECUTED`，exit code 0，耗时 118.27 s：

```text
python -m swebench.harness.run_evaluation \
  -d <dev.jsonl> -s dev -i pvlib__pvlib-python-1072 \
  -p <prediction.jsonl> -id <run_id> --max_workers 1 -t 1800 \
  --report_dir <eval/run_id> -n swebench --cache_level instance
```

官方报告的字面结果：

| 字段 | 值 |
|---|---|
| total / submitted / completed | 1 / 1 / 1 |
| resolved / unresolved | 0 / 1 |
| empty_patch / error | 0 / 0 |
| patch_successfully_applied | true |
| **literal official verdict** | **UNRESOLVED** |

官方报告把 FAIL_TO_PASS 的
`pvlib/tests/test_temperature.py::test_fuentes_timezone[Etc/GMT+5]` 和 17 条
PASS_TO_PASS 全部记为 failure。由于 pytest 在 conftest 阶段就退出，这些「failure」
不是测试断言结果；`error_instances=0` 只说明 Harness 完成了容器/命令执行，不说明
测试环境有效。

**关键限制（必须与判分一起读）**：官方测试输出在 `conftest.py` 导入 `tables` 时就失败，
发生在任何测试收集之前：

```text
ImportError while loading conftest '/testbed/pvlib/tests/conftest.py'.
pvlib/tests/conftest.py:95: import tables
tables/utilsextension.pyx:1: in init tables.utilsextension
ValueError: numpy.dtype size changed, may indicate binary incompatibility.
Expected 96 from C header, got 88 from PyObject
```

为排除「模型补丁导致」这一解释，本轮用**未打补丁的官方镜像**做零模型成本对照：
在 `--pull=never --network none` 容器内直接执行同一测试文件，仍然在同一处
`tables`/numpy 导入失败（镜像 `/testbed` HEAD 为 `dc81df7...`，不是本题 base
`04a523f...`；对照不涉及模型补丁）。证据：
`artifacts/phase16/live-evidence/base-control.log` 与
`artifacts/phase16/live-evidence/eval/.../test_output.txt`。

因此：

- 官方 Harness **确实执行并给出了报告**；字面 verdict 是 `UNRESOLVED`。
- 但该 verdict 是**评测环境失效（INVALID_ENV）**，不是补丁行为的有效判分。不能把它当作
  模型解决/未解决的有效结论，也不能据此声称 `RESOLVED` 或有效的 `UNRESOLVED`。
- 内部 M2 与官方 Harness 被**同一个** `tables`/numpy 问题阻断：内部是目标测试
  `COLLECTION_ERROR`，官方是 conftest `ImportError`。
- 按本轮约定，不为追求成功而修复镜像后重跑；不为结果不理想而重跑模型。现场保留。

---

## 4. 内部验证闭环汇总

| 环节 | 是否执行 | 结果 |
|---|---|---|
| 补丁存在/可应用 | 是 | 3,736 B；PatchValidator 6/6 通过；官方容器 `patch_successfully_applied=true` |
| 目标测试 | 否（闭环） | 4 次 coder `run_tests` 与 4 次复现器尝试均 `COLLECTION_ERROR`；目标测试节点 `failed:tests_failed` |
| 回归 | 否 | 目标测试失败后不满足执行条件；`regression=null` |
| 独立审查 | 否 | `review_calls=0`；未执行 |
| 停止原因 | — | `tests_failed`；未执行阶段已逐项记录 |
| 官方判分 | 是（但环境失效） | 字面 `UNRESOLVED`；`INVALID_ENV`（`tables`/numpy binary incompatibility） |

**不把内部 `failed` 改写成官方结论，也不把官方 `UNRESOLVED` 当成模型效果。** 两个口径
分开记录；本轮没有任何有效的模型解决率读数。

---

## 5. 实际执行的定向测试

全部在 WSL2 Ubuntu-24.04、项目 `.venv`、Docker guest socket 下执行；没有全量 pytest。

### 5.1 受影响路径离线集合

```bash
DOCKER_HOST=unix:///mnt/wsl/docker-desktop/shared-sockets/guest-services/docker.proxy.sock \
.venv/bin/pytest -q -p no:cacheprovider -m "not llm and not docker" \
  tests/unit/llm/test_dispatch_budget.py \
  tests/unit/llm/test_dispatch_ledger_reconciliation.py \
  tests/unit/llm/test_glm_provider.py \
  tests/unit/settings/test_phase14_settings.py \
  tests/unit/settings/test_phase15_routing_matrix.py \
  tests/eval/test_swebench_harness.py \
  tests/eval/test_swebench_runner.py \
  tests/sandbox/test_swebench_env.py \
  tests/integration/test_swebench_smoke.py
```

- 首跑：`328 passed, 1 failed`，exit **1**。唯一失败是新 WSL 站点没有默认
  `cache/swebench` 快照，测试在真实 Loader 处报「快照不存在」；这是测试环境路径问题，
  不是代码失败。
- 处理：把既有冻结快照链接到该站点（`cache/swebench -> /root/cp-run/CodePilot-Repair/cache/swebench`），
  不下载、不换 revision。
- 复跑：**329 passed, 1 deselected in 35.46 s，exit 0**。
- `1 deselected` 为 `llm` 标记用例；本轮没有真实 LLM 单测。
- 证据：`targeted-offline-first.log`、`targeted-offline.log`。

### 5.2 真实 Docker daemon 预检查

```bash
DOCKER_HOST=unix:///mnt/wsl/docker-desktop/shared-sockets/guest-services/docker.proxy.sock \
.venv/bin/pytest -q -p no:cacheprovider -m docker \
  tests/sandbox/test_swebench_env.py::test_real_docker_daemon_preflight_is_observable
```

**1 passed in 1.42 s，exit 0**。只读检查，不 pull/build/创建容器。

### 5.3 静态检查

```bash
.venv/bin/ruff check src/codepilot/sandbox/swebench_env.py \
  src/codepilot/sandbox/docker.py src/codepilot/llm/dispatch_budget.py \
  src/codepilot/settings/service.py src/codepilot/eval/swebench_harness.py \
  scripts/swebench_smoke.py
.venv/bin/ruff format --check <同一组文件>
.venv/bin/mypy src/codepilot/sandbox/swebench_env.py \
  src/codepilot/llm/dispatch_budget.py src/codepilot/settings/service.py \
  src/codepilot/eval/swebench_harness.py
```

- `ruff check`：`All checks passed!`，exit **0**
- `ruff format --check`：`6 files already formatted`，exit **0**
- `mypy`：`Success: no issues found in 4 source files`，exit **0**

本轮没有生产代码修改；上述检查覆盖实际执行的路径，不冒充全仓门禁。

---

## 6. 费用、预算与账本

| 阶段 | 请求/调用 | 实际费用 CNY | 硬上限 CNY | 未知费用调用 | 账本状态 |
|---|---:|---:|---:|---:|---|
| 最小真实推理 | 1 | **0.0000688** | 0.50 | 0 | settled |
| Live M2 | 32 | **0.20508872** | 3.00 | 0 | 32/32 settled，`frozen=false` |
| **合计** | **33** | **0.20515752** | — | **0** | 两本账一致 |

- 推理账本：`artifacts/phase16/cost-ledger.json`（`status=settled`，
  `reserved_ceiling_cny≈0.000864`）。
- 两阶段合并账本：`artifacts/phase16/phase16-cost-ledger.json`（含各阶段
  cap、未知费用、派发账本与官方 `INVALID_ENV` 标记）。
- Live 图预算：`0.20508872 / 3.00`，`cost_hard_cap_hit=false`；
  `max_total_tokens=1,000,000` 实际 354,963 input；墙钟 540.97 s（图 419.63 s，
  官方 Harness 118.27 s）。
- 派发账本：`limit=3.00`，`accounted_or_reserved=0.2050887200000001`，
  `frozen=false`，32 条全 `settled`，模型全部为 `glm-5.3-flash`；与 Usage 精确相等。
- 没有花掉 Reviewer 预算（0 次审查调用）；没有触顶任何预算。
- 官方 Harness 的 Docker 镜像/容器/网络资源另计：pvlib 官方镜像 5.9 GB，镜像保留，
  未折算成金额；这不是 token 费用，未混入上表。

---

## 7. 资源清理与凭据边界

### 7.1 容器与卷

| 项 | 运行前 | 运行后 | 结论 |
|---|---|---|---|
| 容器名集合 | 22 个 | 22 个 | 完全一致，无新增/删除 |
| 卷数量 | 26 | 26 | 无新增/删除 |
| `codepilot=1` 临时容器 | 0 | 0 | M2 沙箱容器已清理 |
| `sweb.eval.*` 容器 | 0 | 0 | 官方 Harness 日志明确 `removed` |
| pvlib 官方镜像 | 不存在 | `sha256:c8e43b…` | 有意保留以便审计/复现 |

- 官方 Harness 创建的 `sweb.eval.pvlib__pvlib-python-1072...` 容器在判分后
  stop + remove（`run_instance.log`）。
- 没有执行 `docker system prune`、`docker rmi`、卷删除、容器批量停止或 Docker Desktop
  重启；其他项目的 22 个容器（aegis、traceinquest 等）在前后清单中同名同数量。
- 因为本轮没有重启 Docker Desktop，「重启后其他项目资源的恢复」为 **N/A**，没有需要
  恢复的动作；`pre-run.txt` / `post-run.txt` 是这一结论的直接证据。若未来确实需要重启，
  先按同一清单记录容器与 restart policy，再恢复。
- 证据：`pre-run.txt`、`post-run.txt`、`live-summary.json`、`eval/.../run_instance.log`。

### 7.2 凭据与证据

- Settings 只保存 `credential.kind=env`、`name=GLM_API_KEY`；没有把 Key 值写入设置文档、
  任务快照、artifact 或日志。
- 对 `artifacts/phase16/` 下 53 个证据文件做了精确值扫描：**0 命中**；没有复制 `.env`
  （`find` 结果为空）。扫描只输出文件路径，不输出 Key。
- WSL 侧 `.env` 是 Windows `.env` 的符号链接，运行时读取，未复制到 WSL 仓库。
- 历史 artifact、Phase 11–15 产物、设置库备份均未被覆盖。

---

## 8. Git 状态与剩余问题

### 8.1 本轮结束时

- 分支：`codex/phase12-real-issue-pilot`
- HEAD（实验与证据生成时）：`d5734ada4e079e9b63ccbb95dcf4cf1b34a53786`
- 工作树：本轮只改文档（`docs/notes/phase16-live-m2-delivery.md`、
  `docs/04-分阶段开发任务清单.md`、`docs/05-精细化待办清单.md`、
  `docs/notes/phase15-m3-execution-plan.md`、`docs/project-summary.md`）；
  `artifacts/phase16/`、`.playwright-mcp/` 为未跟踪/忽略产物。未 commit、未 push、未建 PR。
- WSL 站点 `/root/cp-run/phase16/CodePilot-Repair` 为同一 HEAD 的全新克隆；
  未同步 Windows 文档修改（Live M2 不需要）。

### 8.2 剩余问题（按优先级）

1. **P0 官方镜像环境**：`swebench/sweb.eval.x86_64.pvlib...` 内 `tables` 与 numpy 二进制
   不兼容（`numpy.dtype size changed`），导致官方 conftest 与内部目标测试同点失败。
   这是本轮唯一阻断真实判分的因素；需要重建/修复官方实例镜像或在其环境内固定兼容版本。
   修复后若要做有效官方判分，必须作为**新的一次、另行授权**的实验，不能把本次
   `UNRESOLVED` 改写成有效结论。
2. **P1 内部目标测试授权**：目标测试不可执行时，闭环以
   `no authorized target test available` 停止，回归/审查不执行。该行为本轮是如实的，
   但错误分类可以更明确地区分「测试环境 collection error」与「没有测试目标」；
   不在本阶段擅自改代码。
3. **P2 Docker 命名管道的可复现性**：旧失败当前不可复现；同一 SDK/CLI 已恢复。若再次
   出现 `winerror=5`，按最小影响重启 Docker Desktop，并在重启前记录容器与 restart policy。
4. 没有有效的官方解决率读数；M3 的 L2（10 题校准）、L3（test50）、L4（M4/test300）
   **仍未启动**，需要独立授权与有效环境。

---

## 9. 证据索引

主证据目录：`artifacts/phase16/live-evidence/`

| 文件 | 内容 |
|---|---|
| `report-smoke.json` | Live M2 最终 smoke 报告（官方/内部双口径） |
| `report-state.json` / `report-state.md` | 内部 M2 报告 |
| `state_final.json` | 图最终状态（计划、补丁、预算、事件） |
| `events.jsonl` | 节点与工具时间线 |
| `llm_calls.jsonl` / `llm_requests.jsonl` | 逐请求录制（已脱敏，32 次） |
| `patch_final.diff` | 最终补丁 |
| `prediction.jsonl` / `prediction.manifest.json` | 官方预测与 hash |
| `eval/` | 官方 Harness 报告、`eval.sh`、`test_output.txt`、容器日志 |
| `usage.json` / `cost.json` / `dispatch-ledger.json` | 用量、费用、派发账本 |
| `live-summary.json` | 账本/清理/哈希汇总 |
| `source-identity.json` | base commit 与单提交浅镜像身份 |
| `pre-run.txt` / `post-run.txt` | 容器与卷前后清单 |
| `base-control.log` | 未打补丁官方镜像的零成本对照 |
| `minimal-inference-*.json` | 最小真实推理 |
| `cost-ledger.json` | 推理阶段账本 |
| `phase16-cost-ledger.json` | 两阶段合并账本与总费用 |
| `provider-preflight-dockercheck.json` | Docker SDK 恢复证据 |
| `targeted-offline.log` / `targeted-docker.log` | 定向测试 |
| `targeted-offline-first.log` | 首跑的环境路径失败现场（未改写） |

WSL 原始现场：`/root/cp-run/phase16/CodePilot-Repair/artifacts/phase16/live/`；
账本：`/root/cp-run/phase16/dispatch-ledger.json`。
