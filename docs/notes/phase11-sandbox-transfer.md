# Phase 11R｜推理沙箱文件传输改造（宿主 bind → Docker API 传输）

本轮范围：只修首次真实实验暴露的 P0 基础设施缺陷，零模型成本验证。**没有**调用任何真实 LLM，
**没有**重跑首次付费实验，历史实验结论保持 `NOT_EVALUATED` 未被改写。

## 1. 原始故障与运行环境成因

首次真实单题实验（`artifacts/phase11-glm10-20261019/`）在 `reproduce` 节点停止：

```text
DOCKER_OR_CONTAINER_ERROR: invalid mount config for type "bind":
bind source path does not exist:
  …/reproduction-base/repro-base-02
```

同一次执行还报 `无法读取 JUnit XML: /tmp/codepilot-out-*/junit.xml`。

原因不是路径写错，而是拓扑：本机 WSL2 里没有原生 dockerd（`/var/run/docker.sock` 不存在、
service inactive），只能通过 Docker Desktop 预置的 guest socket 连**Desktop 自己的引擎**。
bind 挂载的源路径由 daemon 侧文件系统解析，而该引擎看不见调用方所在的 WSL 文件系统。
实测三种写法全部不可用（`artifacts/phase11-sandbox-hardening/bind-probe` 结论）：

| bind 源写法 | daemon 行为 |
|---|---|
| `/root/…`、`/tmp/…`（WSL 本地路径） | 静默挂成**空目录**，容器看不到任何文件 |
| `/mnt/c/…` | 同上（Desktop 文件共享未开） |
| `C:\…`（Windows 原生形式） | 直接 `invalid volume specification` |

因此凡是依赖宿主 bind 的代码——`DockerSandbox` 的 `/workspace` 与 `/out`——在这条通道上必然失败。
既有文档只覆盖过官方 Harness 自身的挂载策略（`docs/notes/phase11-runtime-acceptance.md` §5.2：
「挂载全部由官方 harness 自身的既有策略决定」），推理沙箱从未被真实验证过。

## 2. 新传输架构

```text
创建两个命名卷 → staging 容器上传工作区归档 + 移交卷所有权 → 删除 staging
   → 创建测试容器（只读根，挂这两个卷）→ start → wait(超时)
   → get_archive("/out") 安全解包到任务专属目录 → 解析 JUnit → 删除容器与卷
```

三条实测约束决定了这个形状（证据：`transfer-probe.json`、`transfer-probe2.json`、`transfer-probe3.json`）：

1. **daemon 对只读 rootfs 的容器一律拒绝 `put_archive`**（400 `container rootfs is marked read-only`），
   与目标路径是不是 tmpfs/卷无关 → 上传必须由另一个可写容器代做，测试容器本身保持只读根。
2. **tmpfs 内容随容器退出消失**，`get_archive` 取不到东西 → 产物必须落在命名卷上，才能在
   `container.wait()` 返回之后回读。
3. **命名卷不依赖 daemon 看见宿主路径**：`HostConfig.Binds` 形如 `卷名:/workspace:rw`，
   `Mounts[].Type == "volume"`。

### 安全边界：一项收紧、一项折衷

- **收紧**：工作区以前是 `mode: rw` 的宿主 bind，现在卷在测试容器里同样可写，但上传与
  所有权移交都发生在一个**不执行任何仓库代码**的 staging 容器里（`user 0:0`、`network none`、
  `cap_drop ALL` + `CHOWN,FOWNER,DAC_OVERRIDE`、`pids_limit 128`、内存 1 GiB、用完即删）。
  这三项能力各自必要：CHOWN/FOWNER 把卷根移交成容器内 uid，DAC_OVERRIDE 用于删掉卷里
  **属主不是 root** 的预填内容（少了它 `find -delete` 直接 `Permission denied`，实测）。
- **必须清空预填**：Docker 在容器启动时会把**镜像在该挂载路径下的内容复制进新命名卷**
  （volume population）。实测把新卷挂到官方实例镜像的 `/testbed` 上，卷里立刻出现镜像自带的
  `.git`，其 HEAD 是 base commit **之后一个**提交（`git rev-list base..HEAD` = 1）——
  也就是可能含修复提交的对象库。staging 因此在上传前 `find <卷根> -mindepth 1 -delete`
  并校验为空，清不掉就 `TransferError` 终止，**拒绝在残留镜像内容上跑测试**。
  这条由 `tests/docker/test_transfer.py`（S-24）用真实容器断言守住：用一个不含构建期文件的
  工作区跑测试，容器里必须看不见那些文件、也看不见 `.git`。
- **折衷**：staging 容器必须 `read_only=False`（daemon 的硬约束，见上）。它只接收我们自己的
  tar 并执行一条固定 `chown`，不跑被测代码、没有网络，生命周期不超过一次上传。
  测试容器的六项契约（`network_mode=none`、`user=1000:1000`、`read_only=True`、
  `cap_drop=ALL`、`no-new-privileges`、CPU/内存/PID 上限与超时/清理策略）全部不变，
  并由真实 daemon 的 `inspect` 与容器内探针双向核对。

### 传输安全规则（`src/codepilot/sandbox/transfer.py`）

上传侧：

- 排除 `.git`、`eval`、`.env`、`.env.local`、`.git-credentials`、`.aws`、`.codex`、`.npmrc`、
  `artifacts`（任一路径段命中即跳过），Gold/官方测试补丁/隐藏测试材料不进容器；
- 归属统一改写为容器内 uid/gid 1000，宿主 uid 不外泄；保留权限位（含可执行位）与空目录；
- 符号链接只允许指向工作区内目标，并在容器内改写成**相对**链接；逃逸目标直接 `TransferError`；
- 非常规文件（fifo/socket/device）一律 `TransferError`；校验失败即终止，不降级回宿主挂载。

回传侧（容器产出 = 不可信输入）：

- 拒绝绝对路径条目、`..` 穿越、任何 symlink/hardlink/fifo 条目；
- 落点必须解析进任务专属输出目录之内；
- 流式与逐文件两级体量上限（总量 128 MiB、单文件 32 MiB），超限先拒绝再解包；
- `get_archive` 以请求目录名为归档根，解包时剥掉该层。

### 归因保持可区分

| 故障 | 状态 / error_reason |
|---|---|
| 容器创建或启动失败 | `SANDBOX_ERROR` / `DOCKER_OR_CONTAINER_ERROR` |
| 工作区归档上传失败 | `SANDBOX_ERROR` / `DOCKER_OR_CONTAINER_ERROR`，detail 前缀「工作区传输失败」 |
| 结果归档回传失败 | 同上，detail 前缀「结果归档回传失败」；若同时超时则仍记 `TIMEOUT` + `post_timeout_error` |
| 跑完但没有 JUnit | `SANDBOX_ERROR` / `MISSING_OR_INVALID_JUNIT`（不会判成 PASS） |
| 容器超时被 kill | `TIMEOUT` / `EXECUTION_TIMEOUT`（回传仍执行，保留部分日志） |
| OOM | `SANDBOX_ERROR` / `OOM_KILLED` |
| 测试真实失败 | `FAIL` / exit 1 |

清理失败改为**累积记账**（kill 失败、容器删除失败、卷删除失败、staging 删除失败同时存在时
不会被后者顶掉），且不参与改写主判定。

## 3. 修改文件

| 文件 | 变更 |
|---|---|
| `src/codepilot/sandbox/transfer.py` | 新增：归档构造、staging 上传、安全回传解包、卷命名 |
| `src/codepilot/sandbox/docker.py` | `execution_container_config` 改收卷名；执行流程改 staging+卷+`get_archive`；`workspace_alias` → `workspace_target`；卷清理与累积清理记账；构建路径归档改为共用规则 |
| `scripts/swebench_smoke.py` | 官方实例沙箱传 `workspace_target="/testbed"`；`PYTHONPATH` 随之指向 `/testbed/src:/testbed` |
| `tests/unit/sandbox/test_transfer.py` | 新增 17 条：归档排除/归属/链接/特殊文件、staging 行为与清理失败、回传解包 hostile 条目与体量上限 |
| `tests/unit/sandbox/test_docker.py` | 假 daemon 复刻「只读根拒收上传」与卷语义；新增/改写 9 条执行路径断言 |
| `tests/docker/test_network.py` | 按角色区分测试容器与 staging 容器的 daemon 证据；断言零宿主 bind |
| `tests/docker/test_user.py` | 直接建容器改用命名卷；新增卷回收与 bind 缺失断言 |
| `tests/docker/test_oom.py` | 独立 OOM 证据改走产品传输路径（卷 + staging 上传） |
| `tests/docker/test_env_cache.py` | 冷构建身份带本轮唯一后缀（镜像缓存活在引擎里，路径复用会让断言在二次运行时莫名成立/失效） |

`src/codepilot/sandbox/paths.py` 的 `docker_bind_source` / `windows_path_to_container_hint`
保留但已无生产调用方；真正的守卫是新增的「零宿主 bind」断言。

## 4. 真实 Docker 零成本验证（无任何模型调用）

现场：WSL2 Ubuntu-24.04（root），代码副本 `/root/cp-run/phase11-sandbox-hardening/code`
（HEAD `55a4380` + 未提交改动，SHA256 见 `artifacts/phase11-sandbox-hardening/sync.sh` 输出），
引擎 Docker Desktop 29.8.2，`DOCKER_HOST` 用既有 guest socket；未拉取任何镜像、未 prune、
未改任何 Docker Desktop 设置。所有探针与验证脚本、日志都落在 `artifacts/phase11-sandbox-hardening/`。

### 4.1 决定架构的三条实测

| 探针 | 结论 |
|---|---|
| `bind_probe.py` | `-v /root/…`、`-v /tmp/…`、`-v /mnt/c/…` 全部静默挂成空目录；`C:\…` 被拒 `invalid volume specification` |
| `transfer_probe.py` / `transfer_probe2.py` | `read_only=True` 时 daemon 对 `put_archive` 一律 400（目标即使是 tmpfs 也一样）；tmpfs 内容在容器退出后 `get_archive` 取不到 |
| `transfer_probe3.py` | 命名卷 + staging 上传可行：只读根仍生效（容器内 `touch /etc/…` 报 Read-only file system）、uid 1000 可写 `/out`、退出后 `get_archive` 取回 junit/stdout/stderr、卷与容器零残留 |
| `population_probe.py` | 新卷挂到镜像已有路径会被**预填**：官方实例镜像给出 `.git`，HEAD `d4f98233…`，`rev-list base..HEAD` = 1 |

### 4.2 传输链路 + 原 Issue 复现

`verify_reproduction.py` 用**生产 `DockerSandbox`**（`workspace_target="/testbed"`）在官方实例
镜像里跑两次真实执行，输入全部来自首次实验归档，未重新请求模型：

- `A_source_probe` → `PASS`：容器内 `/testbed/src/marshmallow/schema.py` 含 base commit 的那一行、
  目录只有本轮上传的内容、`/testbed/.git` 与 `eval/` 不存在；
- `B_model_reproduction` → `FAIL`（exit 1）：首次实验里模型写的复现测试以
  `TypeError: 'NoneType' object is not subscriptable`（`schema.py:894`）失败，
  与 issue 描述及 JUnit、退出码、stdout 一致 → **原 Issue 在容器内复现成立**；
- 推理工作区身份：`HEAD = 2be2d83a…`、`rev-list --all --count = 1`；
- 残留核对见 `volume-residue-check.json`：注入清理失败时会短暂留下卷（因为容器删不掉、卷被占用，
  产品把它记进 `cleanup_error`），容器回收后由产品自身的 `cleanup()` 扫描原样收回，最终卷与容器
  残留为 0。该行为已固化进 `tests/docker/test_timeout.py` 的断言。

### 4.3 门禁

| 命令 | 结果 |
|---|---|
| `ruff check src tests scripts` | All checks passed! |
| `ruff format --check src tests scripts` | 379 files already formatted |
| `mypy src` | Success: no issues found in 170 source files |
| `pytest -m docker tests` | **45 passed / 1 failed**（失败项见 §5.1，本机缺 buildx，改动前后同样失败） |
| `pytest -m "not docker and not llm" --cov` | **3283 passed, 6 skipped, 46 deselected in 933.07s**，覆盖率 **91%**（改造前 3242 passed / 90.60%） |
| `pytest tests/unit/sandbox` | **136 passed**（含新增 `test_transfer.py` 与改造后的 `test_docker.py`） |

一轮串行完整链路的原始输出在 `artifacts/phase11-sandbox-hardening/final-gates2.log`
（`gate-mypy.log`、`gate-ruff.log`、`gate-format.log`、`gate-real-docker.log`、
`gate-offline.log`、`gate-reproduction.log`）。

### 4.4 覆盖到的新分支（真实 daemon + Fake 双层）

Fake 层（`tests/unit/sandbox/test_transfer.py`、`test_docker.py`）：归档排除与归属/权限/空目录/
Unicode/相对符号链接、逃逸链接与 special file 拒绝、`wipe → put → chown` 顺序、预填清不掉即终止、
清理失败不吞掉上传异常、回传路径穿越/链接/超限拒绝、上传失败与回传失败各自归因、超时仍回传、
卷回收与过期扫描、`workspace_target` 校验。

真实层（`tests/docker/`）：S-19 网络与挂载形态、S-20 容器内身份/权限 + daemon inspect 契约、
S-21 OOM（含走产品传输路径的独立 daemon 证据）、S-22 清理失败、S-23 环境镜像缓存、
**S-24 卷不被镜像预填内容污染**（`tests/docker/test_transfer.py`，新增）。

## 5. 已知限制与仍未完成的正式验收

1. **本机缺 buildx**：`tests/integration/test_docker_phase1.py::test_phase1_contracts_in_linux_container`
   用 `docker build --progress=plain`，WSL 侧只有静态 docker CLI、没有 buildx 组件，
   报 `unknown flag: --progress`。**改动前后同样失败**（已在未改动的
   `/root/cp-run/phase11-glm10-20261019/code` 上复现同一错误），与本轮传输改造无关，
   也不属于"绕过"范围：修它要么装 buildx、要么改那条 Phase-1 用例的构建方式，都超出本轮授权。
   CI（原生 Linux runner + buildx）不受影响。
2. **只读根的一处折衷已明确边界**：可写的只有 staging 容器，且它不执行任何仓库代码；
   测试容器的六项契约逐条由 daemon inspect + 容器内探针核对。
3. **卷的瞬时残留只在"删除容器失败"注入下出现**，且被 `cleanup_error` 记录、由过期扫描回收；
   没有引入 `docker volume prune`，也没有动别人的卷（扫描按 `codepilot=1` 标签 +
   `codepilot-` 名字前缀 + 可解析的 `CreatedAt` 三重确认，认不出时间一律不删）。
4. **官方 Harness 未跑**：本轮没有新补丁可判分（历史实验结论保持 `NOT_EVALUATED`，未被改写）。
   传输链路修好后，还需要**一次新的付费单题真实实验**才能给出官方 resolved/unresolved——
   那是独立的授权范围。
5. **未做端到端 M2 全图真实模型验证**：本轮的图级验证使用 FakeLLM + 真实 Docker
   （`tests/graph/test_phase8_docker_e2e.py`、`test_phase9_docker.py` 全部通过），
   证明的是"沙箱不再阻断图"，不等于真实模型跑分。
6. 覆盖率阈值、marker 集合、`skip` 数量均未放宽；没有提交、Push、PR、Merge 或改写历史成绩。

## 6. 是否具备申请下一次真实 GLM 单题实验的条件

工程侧：**具备**。首次实验暴露的 P0 传输阻断已消除，并且有真实 Docker 证据；
官方实例镜像、数据集快照、base commit 源码镜像、Harness venv 都仍在本地，无需下载。

申请时应一并确认的三点：

1. 预算口径不变（¥10 硬上限、派发前预扣、未知费用即冻结）；
2. 运行现场仍用 `--artifact-root` 新目录 + 新 run id，不复用首次实验的 ORACLE/历史产物；
3. 首个真实 `run_tests` 一旦再出现 `SANDBOX_ERROR`，按本轮证据链的归因表判定，不得算作模型失败。
