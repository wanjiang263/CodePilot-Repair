# Phase31 最终报告（R2 主控执行）

日期：2026-11-03。分支 `codex/phase12-real-issue-pilot`，起点 a97620d（= Phase30 交付提交）。
角色：单主控执行 Agent。全部工作在本仓库本地完成：**0 次真实模型请求、0 新增费用、未 push、未开 PR、未启用 Superpowers、无 active 指针、v4 未改动。**

## 1. 提交链

| 主题 | 提交 |
|---|---|
| `<S>` fix: complete phase31 graph fixtures and archive schema v5 | `e56ee0bb246f8fc1f7c207531786fe62c38e730c`（21 文件，+1355/−97） |
| `<C>` feat: archive phase31-candidate-v5 (schema v5, pending) | `63b8fdb8f7e708441bf08600dc2c547e48ba2a26` |
| `<M>` docs: freeze phase31-candidate-v5 into the historical baseline manifest | `a938429cdd278375baddcf15b006f5b8726af49c` |
| receipt | `26472c31482296736659ab9311bb4fced984c2e6` |
| 最终文档 | 见仓库最新提交（本报告 + plan/runtime/static/known-failures） |

## 2. 三个进入风险如何解决

1. **3 个 graph 新失败**（include_tests fixture 契约）：根因是 Phase31 harness 向 `_base_repo` 传入
   `include_tests=include_existing_tests`，而三个测试里的包装函数吞掉了该关键字（TypeError）。
   修复：`test_phase9_regression.py::lf_base(root, *, include_tests=True)` 转发；
   `test_synthetic_prompt_isolation.py` 改 `lambda root, **_`；`test_phase9_docker.py::base_repo`
   同步加 `*, include_tests: bool = True`（本轮 docker 被 deselect，仍保持契约一致）。
   验收：任务书指定命令 **8 passed**（`r1.log`，sha256 9f5ff971…）。
2. **resume locale 依赖**：`test_phase9_resume.py` 四处 `json.loads(...read_text())` 在 GBK 默认编码下
   解码 UTF-8 journal 会失败。修复为显式 `encoding="utf-8"`（:30/:42/:49/:72），worker 侧本就
   `write_text(..., encoding="utf-8")`；未采用"仅设 PYTHONUTF8=1"的替代路径。
3. **archive 全组未复跑**：上轮停在 5 failed/128 passed（首轮证据 `archive-v5-first.log`、
   `archive-complete.log` 均保留未覆盖）。本轮以单条命令整组复跑：**143 passed / 0 failed**
   （`r2-full.log`；任务书指定的 5 文件子命令 73 passed，`r2.log`）。143 = 起点 133 + Phase31 新增
   10 条契约测试。

## 3. 最终测试矩阵（实际计数，隔离环境）

隔离：子进程清除真实 provider 凭据、`PYTHON_DOTENV_DISABLED=1`、cwd 不读 `.env`、Docker 目录从子进程
PATH 移除、`PYTHONUTF8=1 PYTHONIOENCODING=utf-8`（仅环境加固，测试内编码独立修复）。

| 组 | 最终结果（单命令） | 日志 sha256 / junit sha256 | 说明 |
|---|---|---|---|
| tests/graph | **266 passed, 13 deselected, 0 failed**（1827.9s） | 9c3fafd8… / 0a98a8b1… | 与任务书预期 266/13 完全一致 |
| tests/unit | **3055 passed, 2 skipped, 0 failed**（1224.8s） | 605186a5… / 99c89e5b… | skip=mkfifo 平台限制+embedding opt-in（既有） |
| tests/integration | **126 passed, 1 deselected, 0 failed**（481.6s） | b3361f13… / 870b96fd… | deselected=docker |
| tests/api | **113 passed, 0 failed**（177.8s） | 7798c572… / 604c7c11… | |
| archive 全组（10 文件单跑） | **143 passed, 0 failed**（691.7s） | ac38a2ee… / 48739158… | 任务书 5 文件命令 73 passed（dc837ede…/062254c3…），补集 70 passed（e9413757…/cfca9d3b…） |
| R1 验收 | 8 passed, 0 failed | 9f5ff971… | 见 §2 |
| 前端 web/src | 条件跳过 | — | `git diff --name-only 8e1f1f4..HEAD -- web/src` = 0 文件；未把 Phase30 结果当作本轮结果 |

日志/确定性：JUnit XML 与日志 sha256 记录于 `phase31-runtime-results.json`（full_regressions 14 条
含全部首轮失败证据，未从最终计数中重复累加）。

### 首轮失败与处理（如实记录，未掩盖）

- unit 第 1 次：3053 passed + `WinError 1455 页面文件太小`（git init spawn）+ resume 节点 spawn 超时
  ——机器 commit 内存 89%（诊断记录 `memdiag.ps1` 输出 freeVirt 5.4G/48.9G）。第 2 次同一 spawn 节点
  再次超时（3054 passed）。按"同方案连续 2 次失败换路径"：空闲静置后第 3 次**单命令整跑 0 failed**；
  两节点单独复跑均绿（`unit-retry2.log`）。测试文件未改、无超时加长、无 skip。
- integration 第 1 次：125 passed + 1 failed——phase28 loop 测试钉住旧输出串 `new  ok`；v5 校验器对
  schema<4 候选输出 `legacy_non_strict` + reasons（退出码语义不变）。属 Phase31 契约变更未同步的
  过期钉子，更新断言（见 §5），第 2 次 126 passed。
- api 第 1、2 次：112 passed + 1 failed（cleanup `deleted=[]`，Windows rmtree 句柄竞态；节点单跑绿、
  api 目录单文件跑 7/7 绿，无顺序依赖）。第 3 次 113 passed。测试未改。

## 4. 16 项原失败逐条结果

全部 16 项 status=fixed，逐条含首轮 traceback 日志（`baseline-core-elevated.log`、
`baseline-resume-utf8.log`、`baseline-review-budget.log`）、sha256、修复依据与复跑证据，见
`phase31-known-failures.json`（failures 数组 16 条 + final_status 块）。要点：

- **test_phase9_resume.py `test_runs` 5→4**：代码路径 L2（impact）已完整覆盖授权 baseline（单个测试
  文件），其执行即 L3 证据，不再重复整跑——`levels_run` 仍为 `["L1_target","L2_impact","L3_full"]`、
  `complete=True`、`executed_tests=["tests/test_core.py"]`、`unexecuted_tests=[]`；保留
  `usage.llm_calls`、`test_runs`、`review_calls=1`、`attempts` 断言。预算与记账断言未弱化。
- **closure 30 次调用**：9 个 closure 节点 + phase12 admission 的失败均为 Phase26(748ad2f) 引入显式
  `coder_relocalize` 后脚本缺回复。修复补齐只读回复，`coder + coder_relocalize` 合计仍为 30 次本地
  工具调用；独立/全局预算语义断言原样保留。
- **collection error**：不再把 UNKNOWN 归因当作模型诊断入口；测试构造 `src/pkg/core.py:2 SyntaxError`
  的显式脚本结果，提供真实 changed-file traceback 后才进入 diagnose——诊断条件是收紧而非放宽。
- **test_phase8_integration 目标语义**：`select_target_tests` 优先既有 Planner 授权目标
  （`target_test_source == "planner"`、`target_tests == [TARGET]`），同时复现校验仍执行、临时测试不进
  最终补丁（断言保留）。
- **termination**：空 Planner 列表仍允许 impact/baseline/related 回退；真正无测试仓库才 FAILED 且
  不执行——`include_existing_tests=False` 构造。

## 5. 期望修改逐条解释（本轮新增，共 3 组）

1. 三个 graph fixture 包装（§2.1）——跟随 Phase31 harness 的 `include_tests` 关键字契约，纯签名适配。
2. resume 四处 `read_text(encoding="utf-8")`——locale 加固，断言不变。
3. integration loop 钉子 `new  ok` → `new  legacy_non_strict` + `[note] reasons=['schema<4']`——v5 对
   旧 schema 候选给出显式分类（非失败状态）；clean 仍 exit 0、篡改仍 exit 1/`[fail]` 不变。

16 项原失败的期望修改已在 known-failures 逐条给出依据；无 skip/xfail/删断言/超长 timeout。

## 6. 静态检查（范围如实）

对象为本轮全部 21 个修改/新增 Python 文件（17 tracked 修改 + 3 个 R1 fixture 文件 + 1 integration
钉子更新 + 3 个未跟踪 Phase31 单测文件转正；mypy 只对 4 个 src/scripts 文件，不宣称全仓）：

- AST parse 21/21 exit 0；`ruff check` exit 0；`ruff format --check` exit 0（21 files already formatted）
- `mypy`（scripts/phase28_archive.py、scripts/phase28_archive_verify.py、scripts/phase31_receipt.py、
  src/codepilot/archive.py）exit 0
- `git diff --check` exit 2——全部为原 CRLF 文件新增行的 CR-at-EOL 告警（该仓此批文件索引即 CRLF，
  本轮原样保留，未做行尾归一化伪装）；CRLF-aware（`core.whitespace=cr-at-eol`）exit 0；21 文件无混行尾
- 凭据扫描：仅扫新增行（tracked 走 `git diff BASE`，未跟踪新文件全量），1355 行，0 findings，值不打印
- web/src 条件跳过（0 改动），记录在案，不伪称重跑

## 7. 候选 phase31-candidate-v5

生成：`scripts/phase28_archive.py --version phase31-candidate-v5 --parent phase30-candidate-v4
--schema-version 5 --manifest-kind full --diff-base 8e1f1f4… --source-commit e56ee0b…`（未传
--source-list；生成时 tracked 树干净，dirty_worktree=false）。

- 目录仅 4 文件：candidate.json / snapshot.json / sources.json / candidate.diff
- schema_version=5，candidate_schema_version=5，status=pending，parent=phase30-candidate-v4，
  source_commit=e56ee0b…，diff_base_commit=8e1f1f4…，source_manifest.kind=full
- 源集：changed_files=21（与 `git diff --name-status -M 8e1f1f4..<S>` 的范围内文件一一对应）；
  diff 范围 37 文件 = 21 范围内 + 16 scope_excluded（phase29/30 文档与 v4 档案产物）；
  excluded_files=4（.gitignore、historical-baseline.json=archive 元数据、两份 phase28 文档，
  均附范围外理由）——无遗漏；resolved_source_files=107（=91 继承 v2∪v3∪v4 集 + 16 个新文件，
  与 sources.json 107 条一致）
- snapshot：schema_version=1，coder/planner/reviewer prompt 非空（361/1823/496 字符），逐字节继承自
  v4 链（本轮无 prompt 覆盖，如实记录），instruction/experiences 继承；strict parent integrity：
  v4 declared snapshot_file_sha256=55afb0ba… 与实际字节一致（file_bytes_consistent=true；v4 的哈希
  规则即文件字节，canonical_consistent=false 是规则差异记录而非缺陷）
- 哈希：candidate_tree_sha256=`cd58026d0eabb496fdc72ba82de63f5a4e01c7c2a16c6ef201be1a43269c6fb7`；
  candidate.diff `1211a861…`；candidate.json `b97ca878…`；snapshot.json（file）`fae8c29a…`，
  snapshot_canonical `9ea628f7…`；sources.json `867fc341…`；diff_sha256=`1211a861…`
- 无 active.json / active.next.json；v4 目录与状态未动（status 仍 pending）

## 8. Manifest、receipt 与 witness

- `historical-baseline.json`（`<M>`=a938429）由 `scripts/phase29_baseline_manifest.py` 重新生成：
  44 条，含 v5 四文件；旧候选条目与当前文件逐一比对 **0 mismatch**（旧哈希不变；
  zero_change_since_baseline=true，baseline 9ccc703）。
- receipt（26472c3）由 `scripts/phase31_receipt.py` 确定性生成，字段：candidate/candidate_commit=
  63b8fdb…/source_commit=e56ee0b…/diff_base=8e1f1f4…/manifest_commit=a938429…/
  historical_manifest_sha256=16e82a45…/candidate_tree_sha256=cd58026d…/status=pending。
- 外部 witness（仓库外，`.codex-pytest-tmp/phase31/witness.txt`）：
  `d2689f7afcfee18d89c362eb81cfdc87a66d464c187b9dcbed6bcc1fee0595d8` = receipt 文件 sha256。
- 全量校验：`phase28_archive_verify.py --all --ignore-raw-drift --receipt … --expect-receipt <witness>`
  **exit 0**：versions=11（new=4/old=7）、new_failures=0、unexplained=0、
  historical_files_modified=False、new_archive_status=passed、manifest_binding=manifest_commit、
  **witness=verified、historical_integrity=ok**；phase31-candidate-v5 → new ok。
  日志 `verify-all-final.log` sha256 e302ede5…。

## 9. 未验证项与边界（如实）

- 真实 provider 请求 0（fake/simulated 路径）；Bedrock/TLS/真实凭据未验证
- Docker：本轮全部 deselect/PATH 移除，未运行
- 官方 Harness、大仓性能：未跑——性能一栏如实写"本轮未跑"，不沿用 Phase30 数字
- 页面 E2E：web/src 0 改动，条件跳过
- unit/api 各经历 2 次环境性失败后以第 3 次单命令整跑为最终证据（§3），首轮日志保留

## 10. 下一阶段条件

- v5 仍为 pending，需外部 harness 接受后方可转 active（生成 active 指针属于后续阶段）
- witness 有效期绑定当前 manifest 字节；重生成 manifest 即失效（需重新出 receipt，Phase30 惯例）
- 真实 provider/Docker/官方 Harness/大仓性能验证仍是后续阶段的进入条件

## 11. 最终验收清单

```
[x] 3 个 graph 新失败已修，全量 graph 0 failed        — 266 passed / 13 deselected（final-graph.log）
[x] 完整 archive 复跑 0 failed                        — 143 passed 单命令整跑（r2-full.log；任务书 5 文件命令 73 passed）
[x] unit/integration/api 全量 0 failed                — 3055 / 126 / 113 passed（final-unit3/integration2/api3.log）
[x] 静态检查通过，范围如实                            — 21 文件 AST/ruff×2/mypy(4 文件)/CRLF-aware exit 0；凭据 1355 行 0 findings
[x] v5 候选字段、源集、哈希正确                       — schema 5/pending/parent=v4/21+16+4 源集账目/107 resolved/4 文件
[x] manifest 与 v5 一致且已提交                       — <M>=a938429，44 条含 v5，旧条目 0 mismatch
[x] receipt 绑定 manifest_commit，外部 witness 验证   — manifest_commit=a938429；witness=d2689f7a…（仓库外）
[x] verify --all exit 0                               — witness=verified, historical_integrity=ok, new_failures=0, unexplained=0
[x] 最终报告和 runtime/static/known-failures/plan 已更新 — 本文件 + 3 个 JSON + plan.md
[x] 本地提交完成，tracked 干净                        — 提交链 §1；git status 无 tracked 改动
[x] 无 active 指针，v4 未改                           — 无 active.json/active.next.json；v4 status=pending 原样
[x] 真实模型请求 0、费用 0、未 push                   — 全程 fake/simulated；未 push、未开 PR
```
