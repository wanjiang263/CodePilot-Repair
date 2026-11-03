# Phase32 最终报告（单主控执行）

日期：2026-11-03。分支 `codex/phase12-real-issue-pilot`，起点 `0dbddfeef327410aac292ef0e7df7182c5222f02`。
角色：单主控执行 Agent。全部工作本地完成：**0 次真实模型请求、0 元费用、未 push、未开 PR、v1–v5 历史候选与旧 receipt/旧账本未改动、v5 仍为 pending、`configs/harness/active.json` 与 `active.next.json` 均不存在。**

## 1. 提交链

| 主题 | 提交 | 文件 |
|---|---|---|
| docs: correct phase31 source and static counts | `0d9a385` | 2（+34/−6） |
| docs: record phase30 errata | `d2c20bc` | 2（+17/−0） |
| fix: make v5 receipt generator parent-agnostic | `f2d8612` | 2（+162/−7） |
| test: stabilize unit and api resource gate | `b251ac7` | 1（+269） |
| feat: phase32 provider, archive and index validation harness | `03ebbbf` | 8（+1282/−1） |
| docs: record phase32 final report and evidence | 本报告所在提交 | 4（本报告 + 3 个 JSON） |

## 2. 三个主问题

### 2.1 文档计数（T01 -> `0d9a385`）

`docs/notes/phase31-final-report.md` 现在给的是可逐项复算的账目，不再是"91 继承 + 16 个新文件"：

- 源集：v4 源集 **96** → v5 剔除 4 个范围外文件后继承 **92**；`changed_files=21` 中 6 个是 v4 已有文件，**15** 个新文件；**92 + 15 = 107**，与 `sources.json` 107 条一致。
- `16` 被明确标注为 `source_manifest.scope_excluded_changed_files` 的条目数（37 = 21 范围内 + 16 范围外），不是新增源码数。
- 静态文件计数改为 **21 个文件 = 17 个修改 + 4 个新增**；4 个新增是 `scripts/phase31_receipt.py` 与 3 个 `test_phase31_*`；3 个 R1 fixture、integration 钉子、`test_phase9_docker.py` 都在 17 个修改文件内，不重复相加。
- `docs/notes/phase31-static-checks.json` 的 `file_count` 与 21 文件清单同步修正（2 行改动）。

复核命令与结果：`grep -rn '91 继承\|16 个新文件\|17 tracked' docs/` 只剩两处**正确**引用——
`docs/notes/phase30-final-report.md:116`（v4 自己的 96 = 91 继承 + 11 本轮，历史文档，本轮只读）与
`docs/notes/phase31-final-report.md:180`（引用 Phase30 勘误，说的是 v4 的 96 而非 v5）。第三个错误串 `17 tracked 修改 + 3 个 R1` 已无命中。
`docs/notes/phase31-runtime-results.json` 未重复这些计数，因此未改。

### 2.2 phase30-errata（T02 -> `d2c20bc`，选 A）

`docs/notes/phase30-errata.md`（13 行）已入库，并在 `phase31-final-report.md` 增补一段说明：它是 Phase30 的口径勘误（v4 的 96 = 91 继承 + 5 新增、mypy 实检 9 文件、v4 无 manifest_commit），**历史候选字节未改、旧 receipt 未重签**。
未跟踪项的账目：起点约 173 项历史证据，该文件转 tracked 记 -1，本轮新增 4 份 phase32 文档记 +4，docs 提交前为 176；提交后回落到 172，全部是起点就有的历史证据，未删除任何一项。

### 2.3 receipt 生成器通用化（T03 -> `f2d8612`）

`scripts/phase31_receipt.py` 不再写死 `"parent": "phase30-candidate-v4"`：

- `parent` 从 `candidate.json` 读取；显式传 `--parent` 时必须与候选文件严格一致，否则失败。
- `status` 从候选读取且必须为 `pending`；`schema_version`/`candidate_schema_version` 必须为 5。
- `source_commit`、`diff_base`、`candidate` 与 `candidate.json` 交叉校验。
- 原有校验保留：`manifest_commit` 祖先关系、manifest blob 与当前文件一致、候选 4 文件 blob 哈希、`candidate_tree_sha256`、外部 witness 不写入 receipt。

测试：`tests/unit/test_phase31_receipt.py` **27 passed in 260.16s**（`t03-receipt-tests.log`），覆盖 v5 正常生成、不同合法 parent（临时 fixture）、parent 不一致、status 非 pending、schema 非 5、manifest 缺失/错误。
真实 receipt 未被重签：`configs/harness/receipts/phase31-candidate-v5.json` 一字未改（外部 witness 按设计**不写入 receipt**，只由 `--expect-receipt` 绑定），`verify --all` 用 `d2689f7afcfee18d89c362eb81cfdc87a66d464c187b9dcbed6bcc1fee0595d8` 校验仍然 exit 0（§4.1），v5 继续 pending。

## 3. 附带问题

### 3.1 unit/api 环境性失败（T04 -> `b251ac7`）

复现记录（`.codex-pytest-tmp/phase32/test-gate.json` 的 `t04_gate_record` + `t04-resource.log`）：同一命令连跑

| 组 | 第 1 次 | 第 2 次 | 运行后空闲物理内存 |
|---|---|---|---|
| tests/unit | 3065 passed, 2 skipped（1597.74s）exit 0 | **12 failed**, 3053 passed, 2 skipped（1732.87s）exit 1 | 1493 MB → 756 MB |
| tests/api | **1 failed**, 112 passed（221.71s）exit 1 | 113 passed（325.74s）exit 0 | 772 MB → 844 MB |

两次失败的归类不同，分开写：

- **unit 的 12 个失败 = Windows 提交内存耗尽**。节点全在 `tests/unit/test_phase29_archive_security.py`（7 个）、`tests/unit/test_phase29_archive_verify.py`（4 个）、`tests/unit/test_m3_batch.py`（1 个）；错误文本 `AssertionError: fatal: Out of memory, malloc failed (tried to allocate 1048576 bytes)`，来自 pytest 临时目录里的 `git add -A` 子进程返回 128（`gate-unit-run2.log:265`、`:360`），不是断言逻辑错。运行结束后空闲物理内存只剩 756 MB。
- **api 的 1 个失败是清理/捕获时序**：节点 `tests/api/test_phase14_settings_api.py::test_runtime_logs_docs_and_scoped_cleanup`，断言 `cleanup.json()["deleted"] == [task["task_id"]]` 实际得到 `[]`；同时段有守护线程在 capture 关闭后写日志（`ValueError: I/O operation on closed file`）。之后 4 次 api 连跑（`api-diag-loop1..4`）都是 113 passed，该节点没再复现，因此按环境性记录，不宣称根因已定位。

证据完整性说明：T04 门禁的 api 第 1 次日志 `gate-api-run1.log` 被最终门禁的同名 run1（113 passed）覆盖；现存可复核的同节点失败运行是 `final-api.log`（187.63s，1 failed）与 `final-api.xml`（failures=1），两者 sha256 记在 `docs/notes/phase32-runtime-results.json`。unit 第 2 次的日志与 JUnit 完整保留（`gate-unit-run2.log` sha256 `4034e554…`、`gate-unit-run2.xml` `f35ad06f…`）。

修的是**测试基础设施**，不是断言：`scripts/phase32_test_gate.py`（提交 `b251ac7`）

1. 各组串行执行（不再并行抢内存）；
2. 每次运行前后采样空闲物理内存与提交余量，低于门槛（本轮 `--min-free-mb 900 --min-headroom-mb 2000`）直接 exit 3 并记为环境条件，而不是让一组失败看起来像代码回归；
3. 还原 Codex shell 才有的 ripgrep PATH（`tests/unit/retrieval` 13 个用例依赖 `shutil.which("rg")`），记录 rg 路径与版本；
4. 每次运行前清理自己拥有的 pytest 临时根，并记录回收量。

没有新增 sleep、没有加重试装饰器、没有放宽断言、没有加长超时。最终证据是单命令整跑 0 failed（§5）；首轮失败的日志、节点、错误文本与内存读数保留在 runtime JSON 的 `first_round_failures` 与 `t04_gate_record`。

### 3.2 phase28-candidate-v2 的 legacy worktree drift（T05）

`verify --all` 每次都会打印：

```
[note] phase28-candidate-v2 legacy_worktree_drift: ['scripts/phase28_archive.py', 'scripts/phase28_archive_verify.py',
'src/codepilot/archive.py', 'src/codepilot/graph/harness.py', 'src/codepilot/repo/index.py', 'src/codepilot/rsi.py',
'tests/integration/phase28/test_phase28_archive_verify_loop.py', 'tests/unit/test_phase26_closeout.py',
'tests/unit/test_phase26_harness.py', 'tests/unit/test_phase28_archive.py', 'tests/unit/test_phase28_archive_verify.py']
```

共 11 个文件。`--ignore-raw-drift` 时按设计忽略原始工作区漂移；`historical_files_modified=False` 只表示 **manifest 文件本身未被改**，不表示原始工作区零漂移。本轮未修改 v2 的任何历史文件。报告与 `verify-v5-postchange.log` 可互相对照。

## 4. Phase32 主线

### 4.1 v5 外部接受与 activation dry-run（T06）

```
.venv/Scripts/python.exe scripts/phase28_archive_verify.py --all --ignore-raw-drift \
  --receipt configs/harness/receipts/phase31-candidate-v5.json \
  --expect-receipt d2689f7afcfee18d89c362eb81cfdc87a66d464c187b9dcbed6bcc1fee0595d8
```

exit 0（`verify-v5-postchange.log`，sha256 见 runtime JSON）：

```
versions=11 new=4 old=7 new_failures=0 unexplained=0 historical_files_modified=False new_archive_status=passed
manifest_binding=manifest_commit
witness=verified historical_integrity=ok
```

activation dry-run 只作用于临时归档副本（`.codex-pytest-tmp/phase32/activation-dryrun/archive`）：真实指针列表为空，未启用 `--allow-pending` 时被正确拒绝（`pending candidate: explicit --allow-pending required`），v5 候选与 v4 候选的 sha256 在 dry-run 前后一致（v5 `b97ca878…`、v4 `b8465cc5…`）。**真实 `configs/harness/active.json` 未创建。**

### 4.2 真实 provider 最小矩阵（T07）

`PHASE32_REAL_BUDGET_CNY` 未设置、环境里也没有任何 provider 凭据（`OPENAI_API_KEY`/`OPENAI_BASE_URL`/`ANTHROPIC_API_KEY`/`AWS_*` 全 false，`boto3`/`botocore` 未安装），因此**真实请求 0 次、费用 0 元**，三条真实端点行（openai-compatible / anthropic / bedrock）全部标记 `blocked`，不写成"已验证"。

零成本部分照跑，覆盖矩阵里除真实端点外的全部行为：

- pytest 集合 166 passed in 93.58s（9 个 llm 测试文件，JUnit 166/0/0/0）；`fake_rows` 共 5 行：openai_compatible、anthropic、bedrock（stubbed boto3 client）、cancellation、usage-and-cost；
- OpenAI-compatible（fake loopback）：流式 chunk、wire 上的 usage 对账、延迟响应头、总 deadline 内的慢速响应体、无隐藏重试（单次 wire 请求）；
- Anthropic 协议、Bedrock/TLS、取消与费用归因同上走 mock/loopback；
- 未发出的请求不计 `unknown`；已发出但 usage 不可确认保留 `unknown`；完整可信 usage 才结算。

结果表见 `docs/notes/phase32-provider-matrix.json`；真实通道的缺失项在该文件 `real_provider.rows[].missing` 逐条列出（`PHASE32_REAL_BUDGET_CNY > 0`、provider 凭据、boto3/botocore）。

### 4.3 Docker / 官方 Harness（T08）

| 项 | 结果 |
|---|---|
| 官方 Harness 单例 | **PASS**：`psf__requests-3362`，镜像 `swebench/sweb.eval.x86_64.psf_1776_requests-3362:latest`（`sha256:c5dc4ba64a2d97d6cff6b82fe9c0561589df13f23df6883d05e99b630942bbd1`），`from_cache=true`，4.44s（`official-probe.json`） |
| 最小 Docker e2e | **失败，不记通过**：`test_minimal_e2e.py::test_fake_llm_with_real_docker_executes_controlled_fixture` 在真实容器里跑到 fixture 阶段被 pytest-timeout 掐断，exit=1、wall 310.36s（`docker-minimal.log`、`docker-minimal.meta`） |

未拉取任何新镜像；未跑 test50/test300/M4。docker binary 与 daemon 均可用（官方 Harness 案例就是走它们跑的），所以这次超时不是"没有环境"，而是该 e2e 在 5 分钟上限内没跑完——如实记为未通过，未调大超时掩盖。

### 4.4 真实仓库索引性能（T09）

对象：本仓库 `f2d8612` 的 tracked 导出（2427 文件，扫描 2405，实际解析 958），探针文件 `src/codepilot/eval/swebench_runner.py`。冷/无变化/单文件变化三场景 + 一个 body-only 对照（`index-profile.json`，四个场景子进程合计 179.09s = 68.77 + 3.07 + 82.71 + 24.54）：

| 场景 | 模式 | 索引 wall | commit wall | 解析/复用 | 符号 | 边 |
|---|---|---|---|---|---|---|
| cold | complete_closure_v2 | **68.43s** | 0.34s | 958 parsed / 0 reused | 10216 | 151776 |
| nochange | verified_snapshot_v2 | **3.07s** | 0.00s | 0 parsed / 2405 reused | 10216 | 151776 |
| change（新增函数，符号集变化） | complete_closure_v2 | **82.51s** | 0.19s | 958 / 2404 | 10217 | 151822 |
| change_body（仅函数体变化） | complete_closure_v2（fast_mode=True） | **24.49s** | 0.04s | 958 / 2404 | 10217 | 151822 |

冷索引其它读数：DB 95.6 MB（91.2 MiB）、峰值工作集 588.5 MB、解析错误 2。

负收益与旁路（不是只记录）：

1. **单文件"符号集变化"比冷索引还慢**（82.51s > 68.43s，+20.6%）。原因：`Indexer.index` 的保守保护——`old_symbol_ids != new_symbol_ids` 时回退整图重建，在非空库上要先删再插 15.2 万条边，而冷启动是空库批量插入。
2. **仅函数体变化走 fast 路径只要 24.49s**（比回退路径快 3.4 倍），说明快路径本身有效，问题在触发条件：新增一个函数就必然回退。
3. **解析结果没有复用**：两个变化场景内容哈希复用了 2404 个文件（`content_reused=2404`），但 `parsed` 仍是 958——除探针之外的 2404 个文件被重新解析。这是剩余 24.49s 里的主要成本，也是后续可优化的最大一项。
4. 建议（下一轮实现，不在本轮改语义）：把解析缓存容量提上去，或对"仅函数体变化"复用上一棵语法树再只重算该文件的边。

局限：单机、单仓库（2405 文件）、探测编辑是合成写入；结果不能外推到更大仓库或其它硬件。

### 4.5 页面 E2E（T10 条件跳过）

`git diff --name-only 0dbddfe..HEAD -- web/src` 输出 0 行（web/src 75 个 tracked 文件全部零改动），因此**未跑，web/src 零改动**。没有把 Phase30/31 的页面结果当成本轮结果。

## 5. 测试矩阵（最终回归，单命令整跑）

| 组 | 命令 | 结果 | 日志 sha256 | JUnit sha256 |
|---|---|---|---|---|
| tests/graph | `pytest tests/graph -q -m "not docker"` | **266 passed, 13 deselected, 0 failed**（1805.24s） | `55df8da1c5a44434…` | `9434481f022406be…` |
| tests/unit | `phase32_test_gate.py --target unit=… --repeat 1` | **3065 passed, 2 skipped, 0 failed**（1212.67s） | `b482007fdc34f548…` | `5953e47002a71466…` |
| tests/integration | 同上 | **126 passed, 1 skipped, 0 failed**（444.00s） | `08e9a967d8f8a4a3…` | `a086ddd7b76ce51c…` |
| tests/api | 同上 | **113 passed, 0 failed**（186.44s） | `e730e367b382c870…` | `ecfea690f0688798…` |
| archive 全组（10 文件） | 同上 | **131 passed, 0 failed**（503.07s） | `2ab569caeed66e7b…` | `ef0e0b9c0e38ee0a…` |
| 前端 web/src | — | 条件跳过（零改动） | — | — |

完整 sha256、内存读数、命令全文见 `docs/notes/phase32-runtime-results.json`。重叠组不重复相加：archive 的 10 个文件同时也在 unit 组里跑到。

计数差异说明（如实记录）：

- 2 个 skip 是既有平台/opt-in 限制：`tests/unit/retrieval/test_embeddings.py:104`（未设 `CODEPILOT_TEST_EMBEDDING_MODEL`）、`tests/unit/sandbox/test_transfer.py:253`（`os.mkfifo` 平台不支持）。
- `tests/integration/test_docker_phase1.py:68` skip：该用例自己检测到 daemon 未就绪（重启后 Docker Desktop 未随开机启动），不是本轮改动引入。
- archive 131 与 Phase31 的 143 不能直接比：Phase31 那次的 10 文件清单含 `tests/unit/test_phase31_receipt.py`（该文件当时 12 例，本轮经 T03 扩到 27 例，并只在 unit 组统计）。本轮 10 文件 + 该文件目前收集到 158 例，可复算。

## 6. 静态检查（范围如实）

`scripts/phase32_static_checks.py`，11 个本轮改动/新增的 Python 文件，全部 exit 0、`failed=[]`（`docs/notes/phase32-static-checks.json`）：

| 检查 | 结果 |
|---|---|
| AST parse | 11/11 通过 |
| Ruff check | 0 问题 |
| Ruff format --check | 11 文件均已格式化 |
| mypy（改动文件，对照 BASE 的**新增**错误） | exit 0；10 个改动文件里只有 `scripts/phase28_index_profile.py` 有历史错误，`current_errors=17`、`baseline_errors=17`、`new_errors=[]`（脚本先落 BASE blob 再逐条比对消息集合） |
| mypy（src + scripts，仅记录不设门禁） | **173 errors / 24 files**（checked 279 source files）—— 仓库从未对全部 scripts 做过类型检查，未动文件的历史错误不在本轮门禁内 |
| git diff --check / CRLF-aware | 0 问题 |
| EOL（LF only） | 0 CRLF 文件 |
| 凭据扫描（只扫新增行） | 0 命中 |

没有宣称"全仓 mypy 通过"；门禁口径是"本轮改动文件 0 新增错误"。

## 7. 真实请求与费用

| 协议 | 真实请求数 | 成功/取消/超时 | usage 来源 | 费用 |
|---|---|---|---|---|
| OpenAI-compatible | 0 | 0/0/0 | 无（fake loopback 验证） | ¥0 |
| Anthropic | 0 | 0/0/0 | — | ¥0 |
| Bedrock/TLS | 0 | 0/0/0 | — | ¥0 |
| 合计 | **0** | 0/0/0 | — | **¥0**（预算 `PHASE32_REAL_BUDGET_CNY` 未设置） |

未打印任何凭据；日志与 receipt 中无 key。

## 8. 未完成与阻塞

1. **真实 provider 通道 blocked**：缺 `PHASE32_REAL_BUDGET_CNY>0` 与凭据（三个 provider 全缺）、`boto3`/`botocore` 未安装。可执行前置：`set PHASE32_REAL_BUDGET_CNY=20` 并注入 `OPENAI_API_KEY`/`OPENAI_BASE_URL`（或 Anthropic/AWS 对应变量）后重跑 `scripts/phase32_provider_matrix.py`。
2. **最小 Docker e2e 未通过**：`test_minimal_e2e.py::test_fake_llm_with_real_docker_executes_controlled_fixture` 在 310.36s 被 pytest-timeout 掐断（证据 `docker-minimal.log`）。未调大超时、未标 xfail。官方 Harness 单例已 PASS，说明 docker 本身可用。
3. **索引负收益未修**：见 §4.4 第 3/4 条，需要下一轮动解析缓存与 body-only 快路径；本轮只加观测字段 `IndexStats.fast_mode`。

## 9. 下一阶段条件

- v5 激活：需要在外部 harness 侧拿到 witness 确认并显式启用（本仓库侧已 `witness=verified`、`new_failures=0`、`historical_integrity=ok`），激活时不得改动 v4 字节。
- 真实 provider：预算环境变量 + 凭据就位后按 §4.2 矩阵跑真实 4 行；每协议 ≤2 次请求，80% 预算止损。
- 大仓性能：需要 ≥1 万文件的真实仓库与 ≥16 GB 内存的机器，才能把 §4.4 的结论外推。

## 10. 验收清单

- [x] Phase31 报告中的 107、92、15、21、17、4 计数正确 —— §2.1，`0d9a385`，错误串已清零（只剩两处正确历史引用）
- [x] phase30-errata.md 已提交 —— §2.2，`d2c20bc`
- [x] receipt 生成器不再硬编码 parent，测试通过 —— §2.3，27 passed，`f2d8612`
- [x] 真实 v5 外部 witness 仍为 `d2689f7a…`（receipt 未重签，witness 不落盘），`verify --all` exit 0 —— §4.1
- [x] unit/api 最终单命令整跑 0 failed，首轮失败有记录 —— §3.1、§5
- [x] v2 legacy_worktree_drift 已在报告中记录 —— §3.2
- [x] v5 仍 pending、无 active、v4 未改 —— §4.1
- [x] 真实 provider 请求数与费用符合预算；无凭据时明确 0 请求 —— §7
- [x] Docker/官方 Harness 有真实结果或精确阻塞证据 —— §4.3（官方 PASS；最小 e2e 超时如实记录）
- [x] 真实仓库性能三个场景有实际读数 —— §4.4
- [x] 页面 E2E 条件跳过并有 web/src 零改动证据 —— §4.5
- [x] 静态检查通过，范围如实 —— §6
- [x] 本地提交完成，tracked 干净 —— §1、§7 提交表
- [x] 未 push、未 PR、未发布 —— 全程本地
