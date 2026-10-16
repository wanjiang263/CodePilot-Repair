# Phase 10 — Synthetic Benchmark

日期：2026-10-16。基线：`0d3920c`（Phase 9 验收记录提交）；分支：`codex/phase10-synthetic-benchmark`。

## 验收边界

本轮仅本地开发；无 Push、PR、Merge，无真实付费 LLM / Embedding 调用，0 次模型 API 请求。
E-10（真实 LLM 全量 Benchmark）保持 **NOT RUN**。
本轮要建立的是"不会泄漏答案、不会混淆指标、能复现结果"的评测基础设施，不是在合成任务上
拿到好看的成功率；39 个任务的 Issue 语义与"是否泄漏答案"仍只有自动检查与 AI 辅助自查，
真人复核记 **HUMAN_REVIEW_PENDING**（DA-011 / DA-014）。

| 项目 | 状态 |
|---|---|
| E-01…E-09 | 本地实现 + 自动验收完成 |
| E-05/E-06 的 fixture 人工语义复核 | **HUMAN_REVIEW_PENDING** |
| E-10 真实模型全量跑批 | **NOT RUN** |
| 合成成功率 → 真实 Issue 泛化能力 | 不成立，见 DA-012 |

## 实现与证据索引

| 项目 | 实现 | 证据 |
|---|---|---|
| E-01 | `eval/synthetic.py`：严格 schema（`extra="forbid"`）、`InferenceTask` / `EvaluationTask` 视图字段互不相交、`metadata.repo_files` 逐文件 sha256 核对、确定性 base commit（钉定 author/committer 身份与日期 + `--template=`）、`materialize()` 拒符号链接/拒非空目标、`INFERENCE_DENIED_DIRECTORIES={"eval"}` 进 `WorkspacePathPolicy` 拒绝集 | `test_synthetic.py` 42 项：round-trip、视图字段互斥、`eval/` 不复制、哨兵不可达、SHA 不符即拒绝 |
| E-02 | `eval/evaluation.py` + `scripts/validate_fixtures.py`：九态 `ValidationStatus`，先取基线再跑 gold（顺序故意，反过来会把"base 本来就能过"掩盖成 VALID），gold 侧用全量套件跑以便看到声明清单之外的回归；`ENV_ERROR` 既不并入无效也不并入通过，且不进基线缓存 | 真实 Docker 39/39 VALID（见下）；`test_evaluation.py` 24 项含"FakeSandbox 结果不作为有效性证据" |
| E-03 | `eval/metrics.py`：`Ratio(分子/分母/value|None)`、Recall@K / Exact@K、Resolved Rate、First Attempt Resolved、Repair Gain、Repair Success Rate、Self-verified Precision（Phase 6 原样复用）、空/非法补丁率、迭代 mean/median/P90（nearest-rank，不插值）、Cost、Stop Reason 分布；全部纯函数 | `test_metrics.py` 53 项，覆盖 02 §17.2 与 04 FR-16 的 18 个必需用例 |
| E-04 | 指标边界：零任务→`value=None`（绝不返回 100%）、全失败、全成功、环境失败单列并可见分母、多 seed 池化不判重复 task_id | 同上；`self_verified_precision` 的口径单测保留 |
| E-05/E-06 | `benchmarks/synthetic/` 39 个 fixture，5 个仅依赖标准库的宿主包，10 个类别各 ≥3，5 个多文件修复，33 个 Issue 不含 traceback、6 个含真实捕获的库异常；f2p/p2p 由 base/gold 真实 pytest 运行**测得**而非手写 | 校验汇总与逐 fixture JSON 记录；`test_fixtures` 类断言在 `test_synthetic.py` / `test_leakage.py` |
| E-07 | `eval/leakage.py` + `scripts/check_leakage.py`：9 类检查 = E-07 要求的 8 类（Issue 文本、修复指令、答案词汇、metadata、推理投影、workspace 内容、工具可达性、符号链接逃逸）+ 本轮自加的 run artifact 回读污染（起点干净不等于全程干净），逐字 + 归一化匹配 | 39 个 fixture ERROR 0 / WARNING 0；`test_leakage.py` 25 项，每类检查都配注入正控制与合法样本负控制 |
| E-08 | `eval/assembly.py`（只从 `InferenceTask` 装配完整 Phase 9 M2 图，运行前核验推理现场：无 `eval/`、git 元数据自包含、`rev-list --all --count == 1`）+ `eval/runner.py`（seed 绑定身份、派发前预留费用、未知费用不记 0、SQLite journal 带内容摘要并拒绝被改动的记录）+ `cli.py` 的 `bench` 命令 | `test_runner.py` 39 项；隔离装配走生产工厂 `assembly_factory` 而非 stub |
| E-09 | `eval/report.py`：`artifacts/phase10/<run_id>/{results.jsonl,metrics.json,summary.md,manifest.json}`，`evidence` 段写明引擎、`claims_model_capability=false`、覆盖率、验证模式与未知费用数 | `test_runner.py` 产物断言 + 本轮真实 smoke 产物 |

## 真实 Docker 校验（E-02 验收）

`uv run python scripts/validate_fixtures.py --out artifacts/phase10/fixture-validation`，
**39 个 fixture 全部真实容器执行，耗时 953.28s，exit 0**：

| 指标 | 实测 |
|---|---:|
| VALID | **39 / 39** |
| base 上目标测试 FAIL 的任务数 | 39 |
| gold 之后目标测试 PASS 的任务数 | 39 |
| 无新增回归的任务数 | 39 |
| 目标测试（FAIL_TO_PASS）总数 | 74 |
| 回归测试（PASS_TO_PASS）总数 | 491 |
| 环境错误 / 无效 fixture | 0 / 0 |
| 执行后端 | `docker`（`real_execution_evidence=true`，`reused_from_cache=0`） |

每条 fixture 记录含 base 与 gold 两次运行的 artifact 路径、逐测试结论、环境身份
（`docker:<cache_key>:<image_id>`）与补丁摘要，落在 `artifacts/phase10/fixture-validation/fixtures/`（Git 忽略）。

真实容器侧另有 `tests/docker/test_synthetic_suite.py`（`@pytest.mark.docker`）：抽样 5 个任务
逐条真实校验、3 个真实负控制（gold 修不好→`GOLD_NOT_PASSING`；base 本来就对→`BASE_NOT_FAILING`；
镜像装不起来→`ENV_ERROR` 而非任务无效）、以及**容器内**对 gold 新增行的摘要探测。

## 信息隔离与泄漏

- 数据面：`eval/` 从不复制进推理 workspace；符号链接/越界路径在 `materialize()` 就被拒。
- 装配面：推理侧只持有 `InferenceTask`，`gold_patch` / `test_patch` / `fail_to_pass` 不在其字段集合内；
  `InferenceAssembly._assert_isolated()` 在开跑前拒绝任何"仓库里有第二个可达提交"的现场。
- 提示面：`tests/graph/test_synthetic_prompt_isolation.py` 在完整 M2 图上抓取 FakeLLM 实际收到的
  每一个请求文本，断言其中没有哨兵串、没有 `gold.patch`、没有隐藏测试路径；同一文件配一条
  正控制——把 gold 那一行写进 `issue.md` 后，同一套扫描必须抓到，否则"没抓到"毫无意义。
- 边界：自动检查只覆盖结构与字面/归一化形态。改写过的、纯语义的提示性泄漏需要真人读 Issue，
  记 **HUMAN_REVIEW_PENDING**，见 DA-011 / DA-014。

## 指标口径

- 分母口径随行可见：每个 `Ratio` 都带分子、分母与 `value=None` 的情形，零样本不会被读成 100%。
- `resolved` 是三值（True / False / None），`None` 表示评测没能给结论（环境失败、镜像坏了），
  不当作 False，也不进 Resolved Rate 分母，但必须被单独报告。
- First Attempt Resolved 独立评测第 1 轮保存的那份补丁，不从最终结果推断——推断值恒等于 Resolved Rate。
- 总体比例与条件比例分开：Self-verified Precision 的分母只含"Agent 自认成功且评测可判定"的任务，
  `claimed_without_verdict` / `evaluated_without_claim` 各自披露，不为了统一分母改数学定义。
- 抽样与全量验证不混表：`verification_mode` 逐行落到 `results.jsonl`。

## 工程 smoke（FakeLLM + 真实 Docker）

运行：`uv run codepilot bench --suite synthetic --config configs/full.yaml --seeds 3
--suite-root artifacts/phase10/bench-smoke-suite --fake-llm-script-dir artifacts/phase10/fake-llm-scripts`

产物：`artifacts/phase10/bench-6a24cec9ecc7/{results.jsonl,metrics.json,summary.md,manifest.json}`（Git 忽略）。

- 脚本由 `scripts/make_bench_scripts.py` 从 **gold patch** 生成：这是一个读了答案的脚本化 Agent，
  唯一作用是确定性走通 推理 → 容器执行 → 评测 → journal → 报告。
  **这里的任何成功率都不构成模型能力证据**（DA-013）。
- 观测数：3 fixture × 3 seed = 9，**9 completed / 记录 9/9 / 数据完整性 full**，
  墙钟 156.0s（14:19:21→14:21:57），`sandbox_backend=docker`、`real_docker_evaluation=true`。
- 结局分布（脚本化 Agent，数字本身不代表能力）：2 个 `correct` 任务 6/6 RESOLVED，
  1 个 `wrong` 任务 3/3 NOT_RESOLVED 但 Agent 自认成功 ⇒
  Resolved Rate **6/9=0.6667**、First Attempt Resolved **6/9=0.6667**、
  Repair Gain 0.0、Repair Success Rate **0/3**、Self-verified Precision **6/9=0.6667**
  （分母=自认成功且评测可判定；`claimed_without_verdict=0`、`evaluated_without_claim=0`）。
  这一列恰好说明指标不是同义反复：自检与评测在 `wrong` 任务上分离，Precision 被真实拉下。
- 定位指标：File Recall@1 **9/9=1.0**、Localization Exact@1 **6/9=0.6667**、
  Function Recall@1 **分母 0 → value=null**（评测侧不提供符号级 gold，如实显示"无证据"而不是 0%）。
- 迭代 mean/median/P90 = 1.0 / 1.0 / 1.0；Token 与费用全为 0（FakeLLM），
  `cost_complete=true`、`unknown_cost_tasks=0`、`total_llm_calls=63`、平均墙钟 10.30s。
- 产物 `evidence` 段固定写明：`engine="FakeLLM 工程 smoke"`、`engine_is_simulated=true`、
  `claims_model_capability=false`、`evaluation_coverage=full`、`verification_modes=["full"]`；
  `summary.md` 标题即为"FakeLLM 工程 smoke"，第一行是不构成模型能力结论的引用块。
- 本轮抓到并修掉两个真实缺陷：
  1. `BenchmarkRunner` 的推理 run_id 只由 task+seed+identity 决定，而推理现场在运行后刻意保留、
     `WorkspaceManager` 拒写非空目录，导致**第二次全新运行** 9/9 全部 `inference_error`。
     修法是 run_id 带上本次 benchmark 标识，配回归测试
     `test_two_fresh_runs_get_disjoint_inference_run_dirs`。复跑能力不是锦上添花，是底线。
  2. `results.jsonl` 没有携带推理侧的定位预测，`metric_inputs()` 永远传 `localization=None`，
     于是 File/Function Recall@K 在真实运行里恒为"分母 0"。补上
     `TaskRecord.localization_files/-symbols`（**只存预测，不存 gold**，gold 在报告阶段由评测侧
     现算，避免答案路径回流进 journal 与产物），并配
     `test_localization_predictions_reach_recall_metrics`。
- smoke 脚本生成器自身也错过一次真实坑：FakeLLM 的 `candidate_id` 派生自
  `repository_id`，而 `repository_id` 派生自推理仓库路径；生成器最初索引的是自己材料化的
  目录（且漏了 `bench` 把评测工作区嵌在 `work_root/evaluation` 下这一层），于是 Localizer 以
  "Unknown or unadmitted file candidate" 拒绝全部任务，症状是 workflow_error 而不是"脚本写错了"。
  现在生成器与 `cli.py` 共用同一个材料化根目录，并在 `_manifest.json` 里记录该绑定路径。

## 怎么用

```bash
# 1. 构建 / 重建 fixture（改 scripts/synthetic_tasks.py 之后）
uv run python scripts/build_synthetic_fixtures.py

# 2. 真实 Docker 校验：没有这一步的记录，bench 拒绝把 fixture 放进分母
uv run python scripts/validate_fixtures.py --out artifacts/phase10/fixture-validation

# 3. 泄漏审计（--with-workspace 会逐任务材料化推理仓库再检查）
uv run python scripts/check_leakage.py --with-workspace

# 4. 离线工程自检（FakeSandbox，结论不作为 fixture 有效性证据）
uv run python scripts/validate_fixtures.py --fake-sandbox

# 5. 跑分：默认不碰付费模型
uv run codepilot bench --suite synthetic --config configs/full.yaml --seeds 3 \
    --fake-llm-script-dir artifacts/phase10/fake-llm-scripts      # 离线工程 smoke
uv run codepilot bench --suite synthetic --config configs/full.yaml --seeds 3 \
    --allow-live-llm --max-total-cost 20                          # 真实模型（需授权，E-10）
uv run codepilot bench --suite synthetic --config configs/full.yaml --seeds 3 \
    --allow-live-llm --resume artifacts/phase10/<run_id>          # 续跑：已完成观测不重跑
```

`configs/full.yaml` 的两层预算含义不同：`budget.max_cost` 是单任务上限，
`limits.max_total_cost` 是整批上限（派发**前**检查，达到后停止派发、已完成结果保留）；
设了整批上限就必须给 `max_cost_per_task` 正值，否则并发会超发。

产物四份，路径 `artifacts/phase10/<run_id>/`：

| 文件 | 内容 |
|---|---|
| `results.jsonl` | 每个 (task, seed) 一行：身份、三态 `resolved`、First Attempt、自检声称、补丁摘要、迭代、Token/费用、验证模式、推理侧定位预测、错误明细 |
| `metrics.json` | 每个比率都带分子/分母（分母 0 → `value=null`）、按类别与按 seed 的分组、seed 间均值±样本标准差（单 seed 为 `null`）、执行状态计数、`data_integrity` 声明 |
| `summary.md` | 人读汇总；FakeLLM 引擎时标题即为"工程 smoke"，第一行是不构成模型能力结论的引用块 |
| `manifest.json` | 运行身份（代码版本+commit+dirty、配置 hash、LLM 配置 hash、fixture 版本清单）与 `evidence` 防误读段 |

## 历史风险

- **DA-011：HUMAN_REVIEW_PENDING**（fixture 的 Issue 语义与答案泄漏人工复核）。
- **DA-012：OPEN**（合成任务分布不代表真实仓库；synthetic 数字不得当作真实 Issue 泛化证据）。
- **DA-013：MITIGATED**（FakeLLM bench 不构成模型能力证据；标识已内建，真实跑批 NOT RUN）。
- **DA-014：MITIGATED**（自动泄漏检查非语义完整证明；能力边界已写入脚本输出与文档）。
- DA-001～DA-010 状态不变；本轮未关闭任何历史延期项，E-10 等待单独授权。

## 已执行测试与门禁

- 本轮新增测试 195 项：`tests/eval/test_synthetic.py` 42、`test_evaluation.py` 24、
  `test_metrics.py` 53、`test_leakage.py` 25、`test_runner.py` 39、
  `tests/docker/test_synthetic_suite.py` 10、`tests/graph/test_synthetic_prompt_isolation.py` 2。
- 完整离线门禁 `uv run pytest -m "not docker and not llm" --cov --cov-report=term-missing`：
  **2665 passed / 0 failed / 1 skipped / 44 deselected，覆盖率 93%，exit 0，1417.71s**。
  唯一 skip 是既有的本地 embedding 模型验收（未设置 `CODEPILOT_TEST_EMBEDDING_MODEL`），
  本轮没有新增 skip，也没有下调 `fail_under = 80`。
- 静态门禁：`ruff check .` exit 0、`ruff format --check .`（331 files）exit 0、
  `mypy src` exit 0（154 source files）。
- 真实 Docker：`scripts/validate_fixtures.py` 39/39 VALID（953.28s，exit 0）；
  `scripts/check_leakage.py` 39 个 ERROR 0 / WARNING 0（含 `--with-workspace` 逐任务材料化）。
- Docker 标记与图级隔离测试一并运行 `pytest tests/graph/test_synthetic_prompt_isolation.py
  tests/docker/test_synthetic_suite.py`：**12 passed / 161.91s / exit 0**
  （10 项真实容器校验、3 项真实负控制、1 项容器内 gold 不可达探测、2 项图级 prompt 探测）。
- E-02/E-08 的两个缺陷各配一条回归测试（见"工程 smoke"节）；离线全量在修复后重跑，
  不把中途取消的运行计为通过。

## 后续

可以进入 Phase 11（SWE-bench Lite 集成）。前提口径：合成集只用于验证评测基础设施与
信息隔离，真实效果的对外数字必须来自 Phase 11–12 的独立测量，且两套数字分开报告。

## 附录：39 个 fixture 的逐条真实 Docker 结论

来源 `artifacts/phase10/fixture-validation/fixtures/*.json`（每条含 base/gold 两次运行的
artifact 路径、逐测试结论、环境身份 `docker:<cache_key>:<image_id>` 与补丁摘要）。
"target" = FAIL_TO_PASS 数；gold 之后通过数列形如 `n/n`；base 列 = 目标测试在 base 上通过的数量。

| task_id | category | 状态 | target | P2P | gold 通过 | base 通过 |
|---|---|---|---:|---:|---|---|
| budget-categorise-loses-the-rent-branch | regression_bug | VALID | 2 | 13 | 2/2 | 0/2 |
| budget-category-totals-accumulate-float-error | edge_case | VALID | 2 | 13 | 2/2 | 0/2 |
| budget-export-adds-currency-column | cross_module_dependency | VALID | 2 | 13 | 2/2 | 0/2 |
| budget-food-branch-drops-lunch | wrong_condition | VALID | 2 | 13 | 2/2 | 0/2 |
| budget-ledger-add-accepts-nonpositive | missing_validation | VALID | 2 | 13 | 2/2 | 0/2 |
| budget-ledger-refund-accepts-nonpositive | missing_validation | VALID | 2 | 13 | 2/2 | 0/2 |
| budget-member-discount-inverted-condition | wrong_condition | VALID | 2 | 13 | 2/2 | 0/2 |
| budget-recurring-demands-every-keyword | wrong_condition | VALID | 2 | 13 | 2/2 | 0/2 |
| budget-refund-records-a-charge | incorrect_return | VALID | 2 | 13 | 2/2 | 0/2 |
| budget-render-row-ignores-column-order | api_misuse | VALID | 1 | 13 | 1/1 | 0/1 |
| budget-snapshot-shares-the-live-list | state_mutation_bug | VALID | 2 | 13 | 2/2 | 0/2 |
| httpclient-config-snapshot-loses-retries | cross_module_dependency | VALID | 3 | 10 | 3/3 | 0/3 |
| httpclient-config-stops-checking-timeout | regression_bug | VALID | 2 | 10 | 2/2 | 0/2 |
| httpclient-recorder-aliases-the-caller-list | state_mutation_bug | VALID | 2 | 10 | 2/2 | 0/2 |
| httpclient-retries-never-reach-the-transport | cross_module_dependency | VALID | 2 | 10 | 2/2 | 0/2 |
| httpclient-retries-rejects-with-wrong-error | wrong_exception | VALID | 2 | 10 | 2/2 | 0/2 |
| httpclient-zero-retries-makes-no-request | edge_case | VALID | 1 | 10 | 1/1 | 0/1 |
| inventory-box-count-loses-the-last-partial-box | edge_case | VALID | 2 | 17 | 2/2 | 0/2 |
| inventory-can-ship-reimplement-the-limits-rule | cross_module_dependency | VALID | 1 | 17 | 1/1 | 0/1 |
| inventory-describe-crashes-on-unknown-sku | api_misuse | VALID | 2 | 17 | 2/2 | 0/2 |
| inventory-fits-accepts-unlisted-sku | wrong_condition | VALID | 1 | 17 | 1/1 | 0/1 |
| inventory-reserve-raises-with-wrong-error | wrong_exception | VALID | 2 | 17 | 2/2 | 0/2 |
| inventory-restock-accepts-zero-shipment | missing_validation | VALID | 2 | 17 | 2/2 | 0/2 |
| inventory-total-weight-crashes-on-unpriced-sku | api_misuse | VALID | 2 | 17 | 2/2 | 0/2 |
| taskqueue-drain-leaves-tasks-queued | state_mutation_bug | VALID | 2 | 15 | 2/2 | 0/2 |
| taskqueue-pop-takes-from-the-wrong-end | incorrect_return | VALID | 2 | 15 | 2/2 | 0/2 |
| taskqueue-priority-orders-by-name | api_misuse | VALID | 2 | 15 | 2/2 | 0/2 |
| taskqueue-retry-makes-one-fewer-attempt | off_by_one | VALID | 2 | 15 | 2/2 | 0/2 |
| taskqueue-retrypolicy-accepts-zero-attempts | missing_validation | VALID | 2 | 15 | 2/2 | 0/2 |
| taskqueue-store-leaves-stale-render-on-update | state_mutation_bug | VALID | 1 | 15 | 1/1 | 0/1 |
| taskqueue-store-stops-reporting-missing-keys | regression_bug | VALID | 2 | 15 | 2/2 | 0/2 |
| textkit-mean-empty-sample-raises-wrong-error | wrong_exception | VALID | 2 | 8 | 2/2 | 0/2 |
| textkit-page-duplicates-the-offset-rule | cross_module_dependency | VALID | 2 | 8 | 2/2 | 0/2 |
| textkit-split-fields-forgets-to-strip | incorrect_return | VALID | 2 | 8 | 2/2 | 0/2 |
| textkit-spread-inverted | incorrect_return | VALID | 1 | 8 | 1/1 | 0/1 |
| textkit-table-keeps-its-own-split-rule | cross_module_dependency | VALID | 2 | 8 | 2/2 | 0/2 |
| textkit-tail-eats-one-extra-char | off_by_one | VALID | 3 | 8 | 3/3 | 0/3 |
| textkit-title-crashes-on-empty-string | edge_case | VALID | 1 | 8 | 1/1 | 0/1 |
| textkit-window-start-one-past-page | off_by_one | VALID | 3 | 8 | 3/3 | 0/3 |

合计：39 VALID / 0 无效 / 0 环境错误；target 74、P2P 491（按 host 分组为 budget 13、httpclient 10、
inventory 17、taskqueue 15、textkit 8）。5 个多文件任务（`budget-export-adds-currency-column`、
`httpclient-config-snapshot-loses-retries`、`httpclient-retries-never-reach-the-transport`、
`textkit-page-duplicates-the-offset-rule`、`textkit-table-keeps-its-own-split-rule`）与
6 个含真实捕获 traceback 的任务（`httpclient-retries-rejects-with-wrong-error`、
`inventory-describe-crashes-on-unknown-sku`、`inventory-reserve-raises-with-wrong-error`、
`inventory-total-weight-crashes-on-unpriced-sku`、`textkit-mean-empty-sample-raises-wrong-error`、
`textkit-title-crashes-on-empty-string`）分别满足 E-06 的 ≥5 下限。

