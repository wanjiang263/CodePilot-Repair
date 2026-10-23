# Phase 17：SWE-bench 评测环境修复与有效官方判分恢复

日期：2026-10-22。状态：**DONE**。原始 pvlib 官方镜像的 NumPy/PyTables ABI 阻断已定位并修复为独立镜像；
使用 Phase 16 原预测/补丁完成一次有效官方 Harness 评测，官方 verdict 为 **RESOLVED / VALID_EVAL**。
本阶段没有调用任何模型，模型费用 **CNY 0.00**；没有覆盖 Phase 16 的历史结果。

---

## 1. Phase 16 的原始失败证据

Phase 16 的 Live M2 运行本身完成了 32 次真实模型调用，费用 CNY 0.20508872；但官方 Harness
在测试收集之前失败：

```text
ImportError while loading conftest '/testbed/pvlib/tests/conftest.py'.
pvlib/tests/conftest.py:95: import tables
tables/utilsextension.pyx:1: in init tables.utilsextension
ValueError: numpy.dtype size changed, may indicate binary incompatibility.
Expected 96 from C header, got 88 from PyObject
```

- 原始官方报告：1 submitted / 1 completed / 0 resolved / 1 unresolved / 0 error，
  字面 verdict **UNRESOLVED**；由于失败发生在 conftest 导入阶段，Phase 16 将其标为
  **INVALID_ENV**。
- Phase 16 的内部 M2 结果仍是 **failed / tests_failed**：目标测试、回归、审查均未真正执行；
  该记录保持为历史事实。
- 原证据：`artifacts/phase16/live-evidence/`；Phase 16 记录：
  [`phase16-live-m2-delivery.md`](phase16-live-m2-delivery.md)。

Phase 16 的官方 `UNRESOLVED / INVALID_ENV` **没有被改写**。本阶段的新结果是独立的新运行。

---

## 2. 根因：NumPy 2.0.2 与 PyTables 3.9.2 的 ABI 不兼容

在原始镜像内用 `--pull=never --network none` 容器读取到的实际版本：

| 项 | 值 | 证据 |
|---|---|---|
| Python | 3.9.21 | `/opt/miniconda3/envs/testbed/bin/python --version` |
| NumPy | **2.0.2**（conda `numpy-2.0.2-py39heeff2f4_0` + `numpy-base`） | `pip show numpy`、`conda-meta` |
| PyTables | **3.9.2**（PyPI cp39 manylinux wheel） | `pip show tables`、`tables-3.9.2.dist-info/WHEEL` |
| pandas / scipy | 2.2.3 / 1.13.1 | `pip show` / `conda-meta` |
| 镜像 OS | Ubuntu 22.04.5 LTS | `/etc/os-release` |

直接导入失败：

```text
numpy_version 2.0.2
tables_import_error ValueError numpy.dtype size changed, may indicate binary incompatibility.
Expected 96 from C header, got 88 from PyObject
```

### 2.1 镜像构建链

原始镜像的 conda 历史（`/opt/miniconda3/envs/testbed/conda-meta/history`）显示：

```text
# cmd: /opt/miniconda3/bin/conda create -n testbed python=3.9 pandas scipy -y
+defaults/linux-64::numpy-2.0.2-py39heeff2f4_0
+defaults/linux-64::numpy-base-2.0.2-py39h8a23956_0
+defaults/linux-64::pandas-2.2.3-py39h6a678d5_0
+defaults/linux-64::scipy-1.13.1-py39heeff2f4_1
...
```

`configs/swebench/env-specs-4.1.0.json` 对 pvlib 0.7 的定义是：

```text
install:    python -m pip install -e .[all]
packages:   pandas scipy
test_cmd:   pytest -rA
```

该定义没有固定 NumPy 或 PyTables 版本；`pvlib[all]` 的 extras 又会在 pip 阶段安装
`tables`。于是环境最终是 conda 解出的 NumPy 2.0.2 + PyPI 的 PyTables 3.9.2，二者 ABI
不一致。

### 2.2 为什么不能直接升级 PyTables

PyPI 元数据给出了硬约束：

- `tables==3.9.2`：`Requires-Python: >=3.9`，依赖 `numpy >=1.19.0`，是 Python 3.9 可用的
  最后一个版本；
- `tables==3.10.0`：`Requires-Python: >=3.10`，且依赖 `numpy <2,>=1.20.0`；
- `tables==3.10.1`：`Requires-Python: >=3.10`，依赖 `numpy >=1.20.0`；
- `tables==3.10.2`：`Requires-Python: >=3.11`。

即：Python 3.9.21 无法安装任何 `tables 3.10.x`；3.9.2 是唯一可用版本，而它的 cp39
wheel 仍按 NumPy 1.x ABI 构建。因此必须让 NumPy 回到 1.x。证据：
`artifacts/phase17/pytables-version-constraints.log`。

PyTables 3.9.2 的 cp39 manylinux wheel 是按 NumPy 1.x ABI 构建的，和 NumPy 2.0.2 的
`numpy.dtype` 结构不兼容。因此「升级 PyTables」在这个 Python 3.9 镜像里不可行；必须让
NumPy 回到 1.x。

### 2.3 修复依据

- NumPy **1.26.4** 是支持 Python 3.9 的最后一个 1.x 版本；
- 镜像内已安装包的声明约束允许它：
  - pandas 2.2.3：`numpy >=1.22.4,<3`
  - scipy 1.13.1：`numpy >=1.22.4,<2.3`
  - PyTables 3.9.2：`numpy >=1.19.0`
- 因而最小改动是只把 NumPy 固定到 1.26.4，不移动 Python、不移动 pandas/scipy、不修改
  `/testbed` 源码或测试、不修改 pvlib 补丁。

这不是猜测：先在原始镜像的一次性容器中安装 1.26.4，验证
numpy/tables/pandas/scipy 导入、pytest 收集与 17 条 temperature 测试全部通过，再构建
固定镜像。

---

## 3. 修复镜像

### 3.1 Dockerfile

`artifacts/phase17/image/Dockerfile`：

```dockerfile
FROM swebench/sweb.eval.x86_64.pvlib_1776_pvlib-python-1072@sha256:c8e43bebd10d7d2330820af520361dc6adc9642f98c7b7b7c415cca39852ffdd

LABEL org.codepilot.phase17.parent_image="swebench/sweb.eval.x86_64.pvlib_1776_pvlib-python-1072@sha256:c8e43bebd10d7d2330820af520361dc6adc9642f98c7b7b7c415cca39852ffdd"
LABEL org.codepilot.phase17.fix="numpy==1.26.4 for PyTables 3.9.2 cp39 NumPy-1.x ABI"

RUN /opt/miniconda3/envs/testbed/bin/pip install --no-cache-dir --disable-pip-version-check "numpy==1.26.4" \
    && /opt/miniconda3/envs/testbed/bin/python -c "import numpy, tables, pandas, scipy; print('numpy', numpy.__version__); print('tables', tables.__version__); print('pandas', pandas.__version__); print('scipy', scipy.__version__)"
```

- 父镜像以 **digest** 固定；原始 tag `swebench/...:latest` 与 ID 保持不变。
- 修复镜像使用独立命名空间/tag：
  `phase17/sweb.eval.x86_64.pvlib_1776_pvlib-python-1072:phase17-numpy1264`
- 只安装一个固定版本 `numpy==1.26.4`；不修改 `/testbed`、测试、补丁或 Harness 代码。

### 3.2 构建命令

```bash
DOCKER_HOST=unix:///mnt/wsl/docker-desktop/shared-sockets/guest-services/docker.proxy.sock \
docker build --pull=false --network=default \
  -t phase17/sweb.eval.x86_64.pvlib_1776_pvlib-python-1072:phase17-numpy1264 \
  -f artifacts/phase17/image/Dockerfile artifacts/phase17/image
```

构建 exit 0；日志 `artifacts/phase17/image-build.log`；Dockerfile SHA-256
`33be762f1f5f13530e5286369945373515997eaaf55a909bccfd95cde480bb19`。

### 3.3 镜像身份

| 项 | 原始官方镜像 | Phase 17 修复镜像 |
|---|---|---|
| tag | `swebench/sweb.eval.x86_64.pvlib_1776_pvlib-python-1072:latest` | `phase17/sweb.eval.x86_64.pvlib_1776_pvlib-python-1072:phase17-numpy1264` |
| ID / digest | `sha256:c8e43bebd10d7d2330820af520361dc6adc9642f98c7b7b7c415cca39852ffdd` | `sha256:e80d07e55c35282ad03d085a9476376586bddb287c239bea1d69f4d92fe31a82` |
| size | 5,917,247,577 B | 6,014,307,693 B |
| 状态 | **保留，不覆盖** | 新增；标签/父镜像关系见 `artifacts/phase17/image-build.json` |

原始镜像与修复镜像在官方运行后都仍存在；Harness 的清理步骤显示 `Removed 0 images`，
`Unremoved images: 1`（有意保留的修复镜像）。

---

## 4. 环境验证（不调用模型）

### 4.1 修复镜像内的导入

```text
numpy 1.26.4
tables 3.9.2
pandas 2.2.3
scipy 1.13.1
```

`tables.utilsextension` 可导入，不再出现 `numpy.dtype size changed`。

### 4.2 模拟官方 eval 的安装步骤

在修复镜像的临时容器内执行官方 eval 脚本相同的 `pip install -e .[all]`；安装后版本仍为
numpy 1.26.4 / tables 3.9.2 / pandas 2.2.3。官方依赖没有把 NumPy 拉回 2.x。

### 4.3 pytest 收集与真实执行

```text
17 tests collected in 0.06s
17 passed, 5 warnings in 0.25s
```

这是 `pvlib/tests/test_temperature.py` 的真实测试，不是只跑 `--collect-only`，也不是
用 `exit 0` 冒充测试通过。证据：`artifacts/phase17/fixed-image-validation.log`。

---

## 5. 一次有效的官方 Harness 评测

### 5.1 运行身份

| 项 | 值 |
|---|---|
| run_id | `phase17-official-numpy1264-20261022` |
| rerun_of | `swebench-smoke-phase16-l1-20261022-pvlib__pvlib-python-1072-s0-52c2e7b7` |
| instance / split | `pvlib__pvlib-python-1072` / `dev` |
| dataset revision | `6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2` |
| 预测文件 | Phase 16 原文件，未重新生成 |
| 补丁 | `ab393289bf1dc1da0a5c0d7b7f0153d656081d581898ba755b363c30068643a7` |
| 预测 SHA-256 | `35b4a6fe82835e834ad176d501993f4a79b488854d93b0d5b5fa6f0e4f905dfd` |
| 修复镜像 | `phase17/...:phase17-numpy1264`，ID `sha256:e80d07e5…` |
| Harness | `swebench==4.1.0`，单并发，超时 1800 s |
| 模型调用 / 费用 | **0 / CNY 0.00** |

Harness 的隔离镜像选择使用其**正式 CLI 参数**，没有改 Harness 代码：

```text
-n phase17 --instance_image_tag phase17-numpy1264 --cache_level instance
```

实例镜像 key 由 `swebench.harness.test_spec` 解析为
`phase17/sweb.eval.x86_64.pvlib_1776_pvlib-python-1072:phase17-numpy1264`，与构件完全一致。
原始 `swebench/...:latest` 不在本次运行的清理集合中，未被替换。

### 5.2 命令与退出码

```bash
/root/swebench-venv/bin/python -m swebench.harness.run_evaluation \
  -d /root/cp-run/CodePilot-Repair/cache/swebench/princeton-nlp__SWE-bench_Lite__6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2/dev.jsonl \
  -s dev -i pvlib__pvlib-python-1072 \
  -p <Phase 16 prediction.jsonl> \
  -id phase17-official-numpy1264-20261022 \
  --max_workers 1 -t 1800 \
  -n phase17 --instance_image_tag phase17-numpy1264 \
  --cache_level instance
```

- exit code **0**
- Harness 输出：`Found 1 existing instance images. Will reuse them.`
- 运行耗时（Harness 进度条）**31.46 s**
- 容器：`sweb.eval.pvlib__pvlib-python-1072.phase17-official-numpy1264-20261022`，
  运行后已由 Harness 清理（`Unstopped containers: 0`）。

### 5.3 官方报告与测试执行

官方顶层报告 `glm-5.3-flash-main.phase17-official-numpy1264-20261022.json`：

```json
{
  "total_instances": 1,
  "submitted_instances": 1,
  "completed_instances": 1,
  "resolved_instances": 1,
  "unresolved_instances": 0,
  "empty_patch_instances": 0,
  "error_instances": 0,
  "resolved_ids": ["pvlib__pvlib-python-1072"],
  "unresolved_ids": [],
  "error_ids": []
}
```

测试输出（官方 `test_output.txt`）明确显示：

```text
PASSED pvlib/tests/test_temperature.py::test_fuentes_timezone[Etc/GMT+5]
...
PASSED pvlib/tests/test_temperature.py::test_fuentes_timezone[None]
======================== 19 passed, 5 warnings in 0.55s ========================
```

- FAIL_TO_PASS：`test_fuentes_timezone[Etc/GMT+5]` **success**，无 failure；
- PASS_TO_PASS：18 条 **success**，无 failure；
- `patch_successfully_applied: true`；
- 无 conftest/import/collection 错误；测试真实执行，不是仅 Harness 退出码 0。
- 官方顶层报告 SHA-256：
  `b41b2f75a1c07615e9b77e4986e78983b2d982d0baa1e01e04a6e67faeb6458e`
- `test_output.txt` SHA-256：
  `128e532c40d7f99cb73da2bea03b4626a22f178f645ae720e9859a6e150596e6`
- 单实例 `report.json` SHA-256：
  `7e5071a3fd462dcf00eb0a16bcbcff45dd572d07d77a0dc85dca569fd8dd2132`

**环境有效性：`VALID_EVAL`。官方 verdict：`RESOLVED`。** 这是新的、独立的运行；Phase 16
的 `UNRESOLVED / INVALID_ENV` 不被改写。

### 5.4 报告路径的 Harness 行为

该版本 Harness 把顶层报告和 `logs/run_evaluation/...` 写到了**当前工作目录**
（`/root/cp-run/phase16/CodePilot-Repair`），而不是 `--report_dir` 指定的绝对目录。
运行后已把报告和日志**复制**到新 run 目录，并写 `report-location.txt` 记录原路径与哈希；
没有重跑、没有修改报告内容。`post-run.txt` 中运行当时的 `report_present=false` 是这一
路径行为的现场记录，不是判分失败。报告可解析且与测试输出一致。

---

## 6. 新旧运行关联与哈希

| 项 | Phase 16（原运行） | Phase 17（本阶段） |
|---|---|---|
| run_id | `swebench-smoke-phase16-l1-20261022-...-52c2e7b7` | `phase17-official-numpy1264-20261022` |
| 官方 verdict | 字面 `UNRESOLVED` / `INVALID_ENV` | **`RESOLVED` / `VALID_EVAL`** |
| 内部 M2 | `failed` / `tests_failed` | 未重跑；保留 Phase 16 内部结果 |
| 补丁 | `ab393289…` | 同一补丁，未修改 |
| 预测 | `35b4a6fe…` | 同一预测文件，未重新生成 |
| 镜像 | 原始 `c8e43beb…` | 修复 `e80d07e5…` |
| 模型调用 / 费用 | 32 次 / CNY 0.20508872 | **0 次 / CNY 0.00** |

`rerun_of` 与全部输入哈希写入
`artifacts/phase17/official-rerun/phase17-official-numpy1264-20261022/manifest.json`。
两个运行并列保留；没有把旧 `INVALID_ENV` 改成 PASS。

---

## 7. 定向测试与静态检查

本阶段只改镜像构建/验证脚本与配置，没有修改生产 Python 代码或 Harness/Runner 代码。

| 检查 | 命令/对象 | 结果 |
|---|---|---|
| 修复镜像导入 | numpy/tables/pandas/scipy | pass；版本见 §4.1 |
| 修复镜像收集 | `pytest --collect-only pvlib/tests/test_temperature.py` | 17 collected |
| 修复镜像真实测试 | `pytest -q pvlib/tests/test_temperature.py` | **17 passed** |
| 官方 Harness 真实测试 | 使用 Phase 16 预测文件 | **19 passed** |
| Ruff check | `artifacts/phase17/prepare_rerun.py`、`make_cal10.py` | `All checks passed!`，exit 0 |
| Ruff format --check | 同两个脚本 | already formatted，exit 0 |
| Mypy | 同两个脚本 | Success: no issues found in 2 source files，exit 0 |
| Python 语法 | `py_compile` 两个脚本 | exit 0 |
| Shell 语法 | `bash -n` 5 个 Phase 17 shell 脚本 | 全部 exit 0 |

没有运行全量 pytest、完整 Docker 测试集、前端测试或任何模型调用。上述结果全部来自本轮
真实执行；没有把 Phase 16 的历史读数拿来冒充本轮通过。

---

## 8. 费用与预算

| 项 | 值 |
|---|---|
| 模型调用 | **0** |
| 新增模型费用 | **CNY 0.00** |
| 官方 Harness / Docker 资源 | 不计入模型账本；使用已缓存镜像与本地容器 |
| Phase 16 历史费用 | CNY 0.20508872（不变，不并入本阶段） |

本阶段没有派发任何 Provider 请求，没有生成新预测，没有新增模型费用。

---

## 9. 清理与保留

| 项 | 运行前 | 运行后 |
|---|---|---|
| 容器总数（含停止） | 22 | 22 |
| 卷数量 | 26 | 26 |
| `sweb.eval.*` 残留 | 0 | 0 |
| 原始官方镜像 | `c8e43beb…` | 保留，ID 不变 |
| 修复镜像 | `e80d07e5…` | 保留（Harness `Removed 0 images`） |

- 没有执行 `docker system prune`、`docker image prune`、`rmi`、卷删除或容器批量停止。
- 官方实例容器由 Harness 自行停止并删除；`Unstopped containers: 0`。
- Phase 17 开始时 Docker Desktop 已停止；启动后 15 个 `unless-stopped` 容器自动恢复。
  Phase 16 结束时尚在运行的 4 个 `traceinquest` 容器（`w17-linux`、`w17-node`、
  `w17-pg`、`w18-linux`）因 `restart=no` 没有自动回来，已用 `docker start` 恢复为
  Up；没有删除、重建或改动它们。恢复后共 **19 running / 22 total**，与 Phase 16
  结束时的运行数一致。2 个 9 天前已退出的 `aegis-phase2` qdrant 与 5 天前已退出的
  `traceinquest-pg-w8` 保持原退出状态，没有被启动或删除。
- 历史 Phase 16 artifact、原始预测/补丁、费用账本均未覆盖。

---

## 10. L2 准备、风险与下一阶段进入条件

### 10.1 L2 cal10 冻结

已创建 `configs/subsets/swebench-lite-cal10.json`，从 dev30 冻结顺序取 10 个不同 repo
的首题；未运行任何 L2 实例：

- count：10
- file SHA-256：`0a1119db38d578f17ff4f45695a5412b8e2a78459650461e9cdd538aa187a135`
- instance-list SHA-256：`1e8fb7a42078a23757183ae7ceb80ffb495545a3a193ea0d975ccf97156f5c3e`
- 实例：astropy-12907、django-11910、matplotlib-23314、seaborn-3010、flask-4045、
  requests-2148、xarray-4493、pylint-6506、pytest-11148、scikit-learn-12471
- 与 dev30 的交集检查：10/10 存在；顺序等于 dev30 的前 10 个「每 repo 首题」。
- 只用于费用/口径校准，不作为解决率结论；L2 实际付费运行仍需另行授权。
- 计划预算保持：**CNY 3.00/题，批次总上限 CNY 30.00**。

### 10.2 未关闭风险

1. **修复镜像只针对本实例验证**：`tables`/NumPy 组合只验证了 pvlib 0.7 镜像；L2 的 10 个
   repo 可能有各自的 NumPy/PyTables/HDF5 约束，不能直接假设同一修复适用。每个新实例仍需
   先做同样的导入、收集与测试验证。
2. **原始官方镜像仍是坏的**：本阶段保留原 tag，只在独立 namespace 提供修复镜像；任何仍用
   `swebench/...:latest` 的运行仍会得到 `INVALID_ENV`。后续批次 manifest 必须记录实际使用的
   修复镜像 ID。
3. **官方报告路径行为**：当前 Harness 把报告写到 cwd；run 目录中的报告是运行后复制的，
   原始路径记录在 `report-location.txt`。这是可复现的 Harness 行为，不是判分失败；如果
   未来要自动化收集，需在 runner 层固定 cwd 或显式复制。
4. **内部 M2 结果仍为 failed/tests_failed**：本阶段只修复环境并用既有补丁做官方判分，
   没有重跑 Agent；不能把官方 RESOLVED 解释成内部 M2 已通过。两者是不同口径。
5. 没有全量 pytest、没有其他实例的有效官方读数；L2/L3/L4 仍未启动。

### 10.3 下一阶段进入条件

- L1 已完成有效官方评测：**RESOLVED / VALID_EVAL**，但内部 M2 仍记录 failed/tests_failed。
- 进入 L2 前：确认 cal10 清单与哈希不变；为每个 cal10 实例准备可验证的修复镜像/环境，
  记录父镜像、镜像 ID、依赖版本与测试收集证据；书面授权 CNY 30.00 批次上限。
- L2 完成并通过费用/口径稳定性检查后，才可讨论 test50；M4/test300 仍需独立授权。
- 不得用本阶段单题 RESOLVED 推导任何 test50/dev/test300 解决率。

---

## 11. 证据索引

| 路径 | 内容 |
|---|---|
| `artifacts/phase17/original-deps-diagnosis.log` | 原始镜像 Python/NumPy/PyTables/conda 证据 |
| `artifacts/phase17/constraints.log` | conda 依赖约束、PyTables wheel 元数据 |
| `artifacts/phase17/pytables-version-constraints.log` | PyPI 3.9.2/3.10.x 的 Requires-Python 与 NumPy 约束 |
| `artifacts/phase17/numpy126-probe.log` | NumPy 1.26.4 一次性兼容性验证 |
| `artifacts/phase17/image-build.log` | 修复镜像构建日志 |
| `artifacts/phase17/fixed-image-validation.log` | 修复镜像导入、收集、17 passed |
| `artifacts/phase17/image-build.json` | 原始/修复镜像 ID、Dockerfile hash、版本 |
| `artifacts/phase17/official-rerun/phase17-official-numpy1264-20261022/manifest.json` | 新 run 的 `rerun_of`、输入哈希、命令 |
| `.../glm-5.3-flash-main.phase17-official-numpy1264-20261022.json` | 官方顶层报告 |
| `.../logs/run_evaluation/.../report.json` | 单实例官方报告 |
| `.../logs/run_evaluation/.../test_output.txt` | 19 passed 的真实 pytest 输出 |
| `.../post-run.txt` / `report-location.txt` | 清理与报告路径事实 |
| `artifacts/phase17/phase17-summary.json` | 本阶段汇总 |
| `configs/subsets/swebench-lite-cal10.json` | L2 冻结清单 |
| `artifacts/phase17/cal10-freeze.log` | cal10 文件与实例列表哈希 |
