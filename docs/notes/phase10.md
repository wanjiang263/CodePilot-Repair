# Phase 10 — Synthetic Benchmark

初始记录：2026-10-16，基线 `0d3920c`（Phase 9 验收记录提交），分支
`codex/phase10-synthetic-benchmark`。真实实验与 AI 语义审查归档补充于 2026-10-18；
归档时实际分支 `codex/glm-live-hardening`、HEAD `93cfec6a2195`、工作树干净。
同日随后完成 AI 审查发现问题的整改与复验（10 个 REVISE 样本、真实 Docker E-02、含
`--with-workspace` 的 E-07、Browser 逐页复核与定向测试），见“2026-10-18 整改与复验”节。
该轮仍为 AI 复核，不是人工验收；E-10 仍为 PARTIAL。

## 验收边界

2026-10-16 的基础设施开发轮次为 0 次真实模型 API；该历史事实保留。2026-10-18 已另行完成
GLM-5.3-Flash、seed 0、39 fixture 的真实模型试运行与本轮 AI 语义审查。E-10 因仅有单 seed、
尚未满足完整多 seed/正式验收条件，现记 **PARTIAL / ACCEPTANCE INCOMPLETE**，既不是完全 PASS，
也不再是 NOT RUN。合成成功率仍不外推到真实 Issue。

| 项目 | 状态 |
|---|---|
| E-01…E-09 | 本地实现 + 自动验收完成 |
| E-05/E-06 的语义复核 | **AI_REVIEWED + AI_REMEDIATED / RE-VERIFIED**（20/39 抽样；10 条 REVISE 已整改并复验；仍为 AI，非人工验收） |
| E-10 真实模型全量跑批 | **PARTIAL / ACCEPTANCE INCOMPLETE**（GLM-5.3-Flash，seed 0，39/39 完成） |
| 合成成功率 → 真实 Issue 泛化能力 | 不成立，见 DA-012 |

## 2026-10-18 GLM-5.3-Flash 真实模型试运行

本节只记录本次 Flash 批次。首次 GLM-4.7 尝试不并入本报告、不与本次解决率合并；其原始
Artifact 仍按保留政策留在 Git 忽略目录，未覆盖、未删除。

### 运行身份与完整性

| 项目 | 原始记录 |
|---|---|
| run_id | `glm-flash-s0-34b9701bbefe` |
| 模型 / 平台 | BigModel `glm-5.3-flash`；localizer/planner/coder/diagnoser/reviewer 同模型；`reasoning_effort=low` |
| seed / suite | seed `0`；synthetic 39 fixture / 10 类别 |
| 代码 | `93cfec6a2195fec1c96bb74a4339ef4e4f63e30d`，manifest `dirty=false` |
| 配置 | config hash `837214211669c1bff0184116d74f35de`；provider config hash `fa17f1bedb67051bb9fc2753da0ac4af` |
| 预算 | 批次上限 ¥80；单任务 reservation / max cost ¥10；实际记账 ¥1.97880528；未触发预算截停 |
| 时间 | 2026-10-18 15:09:30–16:25:41（Asia/Shanghai）；manifest 墙钟窗口 4571.45s；39 条任务 `runtime_seconds` 求和 **3835.998s** |
| 完整性 | runner completed 39/39；`results.jsonl` v2 共 39 行；verification mode 39/39 `full`；真实 Docker 39/39 |

### 结果与归因

- 独立评测 resolved **37/39 = 94.87%**；First Attempt Resolved 同为 37/39；有效评测覆盖率
  **39/39 = 100%**。未解决：`inventory-can-ship-reimplement-the-limits-rule`、
  `textkit-table-keeps-its-own-split-rule`，原始归因均为 `model_not_resolved`。
- execution state：39 `completed`；attribution：37 `resolved`、2 `model_not_resolved`；Harness / Provider
  输出 / ENV / SANDBOX 错误均 0，空补丁与非法补丁均 0。控制台日志恰有 **434** 条 BigModel
  chat completion `HTTP/1.1 200 OK`，非 200 为 0；`results.jsonl` 的 role 汇总错误也为 0。
- 39 条推理 run report：37 `status=succeeded` 且 `verification_level=m2_verified`；2 条
  `status=failed, stop_reason=tests_failed`，没有 Reviewer 对象。`m2_verified` 表示授权 baseline suite、
  review 与无确认新增失败的项目内边界，不等于外部 Issue 真正解决。
- 类别（任务/解决）：`api_misuse` 4/4、`cross_module_dependency` 6/4、`edge_case` 4/4、
  `incorrect_return` 4/4、`missing_validation` 4/4、`off_by_one` 3/3、`regression_bug` 3/3、
  `state_mutation_bug` 4/4、`wrong_condition` 4/4、`wrong_exception` 3/3。
- Self-verified Precision 必须保持原定义：**35/37 = 94.59%**；分母是 Agent 自认成功且评测可判定
  的任务。另有 `claimed_without_verdict=0`、`evaluated_without_claim=2`，不能改写成 37/39。

### Planner、Reviewer 与计数口径

- `plan_corrections` 从 39 份 run report 复算为 **75**：0 次×1 题、1 次×3 题、2 次×33 题、
  3 次×2 题。`llm_requests.by_role.planner.corrections` 与每题记录一致。
- Reviewer “37 approved / 2 not approved” 的准确含义：37 个成功流程实际运行 Reviewer 且
  `approved=true`；另 2 个流程在 `tests_failed` 停止，`review=null`，因此是“未取得批准”，
  **不是 Reviewer 显式给出 `approved=false`**。
- “38 issues”只统计上述 37 份实际 Reviewer 对象里的 issue 条目：27 题各 1 条、4 题各 2 条、
  1 题 3 条、5 题 0 条，合计 **38**。若在 PowerShell 中用 `@($null).Count` 统计两个
  `review=null` 任务，会各产生一个幽灵计数并得到 40；原始数据没有这两条 Reviewer issue。

### Token、缓存与费用

- 39 份 run report / `results.jsonl` 复算：输入 **2,847,185**、输出 **103,568**、总计
  **2,950,753**、缓存命中输入 **1,033,216**、LLM 请求 **434**；与 `metrics.json` 一致。
- 配置记录的 2026-10-18 官方公开单价（元/百万 token）：fresh input `0.8`、output `2.8`、
  cached input `0.23`。计算为 `(2,847,185−1,033,216)×0.8/1e6 +
  1,033,216×0.23/1e6 + 103,568×2.8/1e6 = ¥1.97880528`。
- ¥1.97880528 是按官方单价得到的**记账估算**；本次 API 实际消耗预付费资源包，不能把该数
  写成现金账单或额外扣款。预算使用比例按记账值 / ¥80 = **2.4735%**。

### 原始证据索引

- 批次四件套与 journal：`artifacts/phase10/glm-flash-s0-34b9701bbefe/`。
- 控制台日志：`artifacts/bench-work/flash-s0-console.log`。
- 39 个推理现场：`artifacts/bench-work/runs/glm-flash-s0-34b9701bbefe-<task>-s0-*/`，含
  `report.json`、`state_final.json`、`llm_calls.jsonl`、`llm_requests.jsonl`、`review/`、checkpoint、
  patch 与测试证据。
- 独立评测：`artifacts/bench-work/evaluation/evidence/glm-flash-s0-34b9701bbefe-*-{baseline,model_first_attempt,model_final}.json`。
- 上述路径均位于 Git 忽略的 `artifacts/`，本轮未删除或覆盖任何历史原始证据。

## 2026-10-18 AI 语义审查（Browser Use）

- 审查主体明确为 AI；本轮按用户指定验收口径，以 AI 抽样审查替代原计划真人流程，不冒充
  HUMAN review。确定性规则为 `category ASC, fixture ID ASC, first 2 per category`，20/39、每类 2 条。
- Browser Use 通过本地只读 HTTP 页面逐条实际访问 20/20；索引、选择 manifest、逐样本 HTML 与
  完整五维判定位于 `artifacts/phase10/ai-semantic-review-20261018/`（Git 忽略）。Git 中不复制
  Gold Patch 或隐藏测试正文。
- 结论：`KEEP 10 / REVISE 10 / REJECT 0`；严重度 `P1 2 / P2 8 / 无问题 10`。8 条 Issue
  存在改写式语义提示；2 条另有验收充分性缺口：currency 来源未定义、逐步 float 舍入 Gold/测试
  对更广边界不足。外部业务真实性全部标为 `UNVERIFIED`。
- DA-011、DA-014 更新为 **AI_REVIEWED / REMEDIATION OPEN**：审查动作已有可追溯证据，但发现的
  10 条 REVISE **在该轮结束时**尚未修改；该轮按当时限制未改 fixture/Gold/测试，也不声称人工
  验收完成。同日随后执行了整改与复验，见下节。

## 2026-10-18 整改与复验（10 个 REVISE 样本）

范围：上节判定为 `REVISE` 的 10 个样本——8 条改写式语义泄漏 + 2 条 P1 契约/测试充分性缺口。
另 10 条 `KEEP` 与其余 19 个未抽样 fixture 未修改。修改只落在权威来源
`scripts/synthetic_tasks.py` 与 `scripts/synthetic_hosts.py`，再用 `scripts/build_synthetic_fixtures.py`
重建这 10 个 fixture 并整体复制回 `benchmarks/synthetic/`；没有直接改生成物。

| fixture | category | 整改内容 | 重建后 E-02（target / P2P） |
|---|---|---|---|
| `budget-render-row-ignores-column-order` | api_misuse | Issue 去定位 | 1 / 13 |
| `inventory-describe-crashes-on-unknown-sku` | api_misuse | Issue 去内部常量 | 2 / 17 |
| `budget-export-adds-currency-column` | cross_module_dependency | 补 `Ledger.currency` 合成契约，重写 Gold/测试 | 6 / 13 |
| `httpclient-config-snapshot-loses-retries` | cross_module_dependency | Issue 去符号与修改范围 | 3 / 10 |
| `budget-category-totals-accumulate-float-error` | edge_case | 明确精度契约，重写 Gold/隐藏测试 | 15 / 13 |
| `budget-refund-records-a-charge` | incorrect_return | Issue 去根因与负号提示 | 2 / 13 |
| `budget-categorise-loses-the-rent-branch` | regression_bug | Issue 去关键词表/唯一位置 | 2 / 13 |
| `budget-member-discount-inverted-condition` | wrong_condition | Issue 去函数名/分支互换 | 2 / 13 |
| `httpclient-config-stops-checking-timeout` | regression_bug | Issue 去 `validate_timeout`/wiring | 2 / 10 |
| `taskqueue-retry-makes-one-fewer-attempt` | off_by_one | Issue 去 `n-1`/range 上界提示 | 2 / 15 |

### 8 条改写式泄漏

只删除或弱化内部实现、目标符号、精确编辑动作与根因叙述，保留可观察行为与必要业务契约：

- render：原 Issue 点名 `COLUMNS`、row renderer 与“按列查值”；改为“同一记录换一个字段构造
  顺序会得到不同行，导出值必须与表头对齐”。
- inventory：原 Issue 点名 `UNKNOWN_NAME` 与 lookup 回退；改为“未登记 SKU 应显示 `Q - unknown`，
  自备 catalog 同样成立，已登记项保留名称”。
- snapshot：原 Issue 点名 `new_client`、`Config.as_dict` 与“两边都要修”；改为“便捷 API 传入
  retries=5 仍为 2，诊断快照缺 retry，调用方期望设置生效且快照准确”。
- refund：原 Issue 说明“应写负 entry、当前写入原值”并点名测试缺口；改为“50 支出 + 10 退款
  应报 40 而不是 60，原支出保留、退款保持同类别”。
- retry：原 Issue 直接给 `n - 1` 与“最后一次通常成功”；改为“允许 3 次时，失败两次后本应成功
  的回调永远到不了成功；额度包含首次调用，成功即停止，耗尽才报错”。
- categorise：原 Issue 说“keyword list 仍包含两者、classification 是唯一位置”；改为
  “900 / Rent April 被归为 large，而 rent 不论金额应归 housing；insurance 仍归 housing”。
- timeout：原 Issue 说 `validate_timeout` 存在、constructor 曾调用、“wiring 丢了”；改为
  “timeout=0/-1 构造成功；超时必须严格为正，非法值应在构造时抛库配置错误”。
- member：原 Issue 点名 `apply_discount`、`MEMBER_DISCOUNT` 并说“两个分支互换”；改为
  “200 总额下会员收 200、访客收 180；会员应享 10% 减免，访客原价”。

### 2 个 P1：契约、Gold 与测试

- `budget-export-adds-currency-column`：按用户确认采用 **`Ledger.currency`，默认 `CNY`**，并明确
  这是本轮新增的**合成契约**、不是从原始 base 推断出的业务事实。base 的 `Ledger` 新增
  `currency: str = "CNY"`；Gold 的导出行使用 `ledger.currency`；Issue 写明导入列顺序
  （category, note, currency, amount）、空账本也要表头、USD/EUR、不同账本连续导出各自保留
  币种且不换算金额。隐藏测试的 F2P 目标由 2 条增到 6 条（测试函数 4→6）：CNY/USD/EUR 参数化
  并逐行核对完整 CSV、默认 CNY、USD→EUR→USD 连续导出、空账本表头；写死 CNY 的 Gold 会在
  USD/EUR 上失败。
- `budget-category-totals-accumulate-float-error`：明确精度契约为——以每个输入金额的十进制
  表示为记账值；允许亚分金额与退款；每个类别精确累加后**只舍入一次**到两位；半分取偶；
  结果与条目顺序无关；返回 float、类别键排序；类别独立舍入（各类别结果之和不必等于全账本
  单独舍入的结果）；空账本返回空映射。新增 task-local 宿主变体 `budget_precision`（不改变其他
  fixture 共用的 budget 宿主）：base 为普通浮点累加，Gold 用 `Fraction` 精确累加后 `round` 一次。
  隐藏测试 15 条 F2P：12 组正/负半分边界（`0.005`、`0.015`、`0.025`、`-0.005`、`-0.015`、
  `2.675` 等）、亚分累计、2 组全排列顺序不变（含 `1e16 + 0.01 − 1e16`）、类别独立与排序、
  退款参与舍入；另有空账本与抵消用例（base 也通过，故不计 F2P）。断言验证行为契约，不是
  为了迎合某一行 Gold 实现。

### E-02 真实 Docker 全量校验（本轮新目录）

`python scripts/validate_fixtures.py --out artifacts/phase10/remediation-20261018/e02 --force --keep-work`，
exit 0，`e02/summary.json` / `summary.md` / `e02/fixtures/*.json` / `e02/work/evidence/*`：
**39/39 VALID**；本次真实执行 39、续跑复用 0；无效 0、环境/沙箱错误 0；base 上目标测试失败 39、
gold 后目标测试通过 39；无新增回归 39；target **91**（原 74；precision 2→15、currency 2→6）、
P2P **491**；耗时 909.87s；环境 `docker:*`；`real_execution_evidence=true`。原
`artifacts/phase10/fixture-validation/` 未被覆盖，逐文件哈希核对未变。

### E-07 泄漏检查（含 `--with-workspace`）

`python scripts/check_leakage.py --with-workspace --json artifacts/phase10/remediation-20261018/e07.json`，
exit 0；`checked_tasks=39`、`errors=0`、`warnings=0`、`skipped_tasks=[]`。边界不变：该工具只覆盖
字面/结构规则，0 命中不能证明语义层无泄漏；语义层靠 Browser 逐页复核补充，且仍不是人工盲测。

### Browser 逐页复核

10/10 被整改样本通过本地只读 HTTP + Chromium（Playwright headless）实际打开，全部 HTTP 200，
每页 14～15 个材料区块（metadata、Issue、Gold Patch、隐藏测试、测试清单与全部可见源码）。截图、
页面 innerText 与 SHA-256、console/page error 记录在
`artifacts/phase10/remediation-20261018/browser-review/`；逐样本判断与证据索引见同目录
`review.md` 与 `verification.json`。复核结论：10/10 的语义泄漏与契约/测试问题在材料层面已解决，
未发现整改引入的新泄漏；外部业务真实性继续标 `UNVERIFIED`。

### 数据一致性

`artifacts/phase10/remediation-20261018/consistency.json`：39 个 fixture、10 个改动、29 个逐文件
哈希未变、类别分布与整改前一致（api_misuse 4 / cross_module_dependency 6 / edge_case 4 /
incorrect_return 4 / missing_validation 4 / off_by_one 3 / regression_bug 3 /
state_mutation_bug 4 / wrong_condition 4 / wrong_exception 3）。每条记录含整改前后内容哈希、
fixture_version、base commit、gold/test patch SHA-256、原始校验 JSON 路径与哈希、新 E-02 校验
路径与结论。`history_files_verified=673`：原 E-02 记录与 GLM-5.3-Flash / GLM-4.7 实验 Artifact
等非 `benchmarks/` 文件逐文件 SHA-256 未变。`audit.py` 另断言 39 个 fixture 的 `repo/` 与权威
来源逐字一致、Issue 以来源定义开头、类别未变。

### 定向测试

`pytest tests/eval/test_synthetic.py tests/eval/test_leakage.py -q` → **64 passed / 3 skipped**；
3 条 skip 均为当前环境不允许创建符号链接（既有环境边界，不是本次改动引入）。日志与退出码在
`artifacts/phase10/remediation-20261018/targeted-tests.log` / `.exit`。落盘重跑时曾遇到一次宿主
临时目录的 `WinError 5`（pytest 在 workspace 临时树内 `os.replace` 被拒），失败日志原样保留为
`targeted-tests.failed-20261018T2349.log`，诊断见 `targeted-tests-diagnosis.md`；原样重跑通过，
未删除测试、未新增 skip、未降低断言。本轮未运行全仓测试门禁、未运行 seed 1/2、未运行新
Benchmark、未调用任何真实模型 API。

### 残余限制

- 仍是 **AI 复核**：没有人类盲测“只看 Issue 能否写出 Gold”，也没有外部业务方确认；不得写成
  HUMAN PASS。语义审查仍是 20/39 分层抽样，本轮只重审被修改的 10 条，不能宣称整个 suite
  无残留语义泄漏。
- 两个 P1 的新契约（`Ledger.currency`、十进制精确累加 + 半分取偶）是本轮明确选择的合成契约，
  解决了“无唯一合理行为”，但不等于外部真实业务规则。
- E-10 仍为 **PARTIAL / ACCEPTANCE INCOMPLETE**：单 seed、无多 seed/正式验收；37/39 resolved
  不构成真实 Issue 泛化能力。
- E-07 的 0 ERROR / 0 WARNING 只覆盖结构与字面/归一化规则，不能写成“已证明无泄漏”。
- Browser 证据来自本地只读页面（内容与 fixture 材料一致），不是线上产品页面。
- 同日另做了一次独立 AI 对抗复核（`artifacts/phase10/remediation-20261018/independent-audit.md`）：
  10/10 均为 `CLEAN / CONSISTENT / ADEQUATE / OK`，无 fixture 级缺陷。另记录 4 个低危覆盖/
  措辞缺口——F1 timeout Issue 写“严格为正/正值可用”，而 Gold 通过 `MIN_TIMEOUT=0.001` 拒绝
  `0.0005`，隐藏测试只覆盖 0/-1/0.5（唯一有评分影响）；F2 snapshot 的 timeout 转发未被断言；
  F3 refund 的类别关联未被断言；F4 discount 的两位舍入未被断言——以及 O1/O2/O4 等观察。
  它们超出本轮“8 条泄漏 + 2 个 P1”范围，修改会再次改变 fixture 版本并要求重跑 E-02/E-07，
  故本轮不扩范围，留作后续补测（F1 优先）。

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

> 以上 953.28s / 74 target 的记录是 2026-10-16 的原始 E-02，属于历史事实。2026-10-18 整改后
> 的 E-02 在独立目录重跑：39/39 VALID、target 91、P2P 491、909.87s，见“整改与复验”节；
> 两者不混用。

## 信息隔离与泄漏

- 数据面：`eval/` 从不复制进推理 workspace；符号链接/越界路径在 `materialize()` 就被拒。
- 装配面：推理侧只持有 `InferenceTask`，`gold_patch` / `test_patch` / `fail_to_pass` 不在其字段集合内；
  `InferenceAssembly._assert_isolated()` 在开跑前拒绝任何"仓库里有第二个可达提交"的现场。
- 提示面：`tests/graph/test_synthetic_prompt_isolation.py` 在完整 M2 图上抓取 FakeLLM 实际收到的
  每一个请求文本，断言其中没有哨兵串、没有 `gold.patch`、没有隐藏测试路径；同一文件配一条
  正控制——把 gold 那一行写进 `issue.md` 后，同一套扫描必须抓到，否则"没抓到"毫无意义。
- 边界：自动检查只覆盖结构与字面/归一化形态。2026-10-18 的 AI 语义抽样已证明自动 0 命中
  不能排除改写式提示；20 条中 8 条建议因语义泄漏改写。该记录是 **AI_REVIEWED**，不是人工验收。
  同日完成了这 8 条 Issue 的改写与 2 条 P1 的契约/测试整改，E-02/E-07 在新目录重跑，并用
  Browser 逐页复核 10/10；结论仍是 AI 复核，不能写成“已证明无泄漏”。

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

- **DA-011：AI_REVIEWED + AI_REMEDIATED / RE-VERIFIED**（20/39 分层 AI 审查；10 条 REVISE 已整改，
  E-02/E-07 已重跑，Browser 已逐页复核 10/10；仍是 AI，不是人工验收）。
- **DA-012：OPEN**（合成任务分布不代表真实仓库；synthetic 数字不得当作真实 Issue 泛化证据）。
- **DA-013：MITIGATED**（FakeLLM 与真实模型数字继续分开；GLM-5.3-Flash seed 0 已跑，但 E-10 仅 PARTIAL）。
- **DA-014：AI_REVIEWED + AI_REMEDIATED**（8/20 命中改写式泄漏，证明自动扫描边界；8 条 Issue 已改写，
  但语义泄漏检测仍依赖 AI 抽样，自动检查仍不是完整证明）。
- DA-001～DA-010 其余状态不变；E-10 为 **PARTIAL / ACCEPTANCE INCOMPLETE**。

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
- 2026-10-18 整改轮定向复验（未重跑全仓门禁）：`pytest tests/eval/test_synthetic.py
  tests/eval/test_leakage.py -q` = **64 passed / 3 skipped**，skip 均为当前环境不允许创建
  符号链接；一次落盘重跑遇到宿主临时目录 `WinError 5`，失败日志保留并定位为环境问题，原样
  重跑通过（`targeted-tests-diagnosis.md`）。真实 Docker E-02 **39/39 VALID**（909.87s，
  target 91 / P2P 491，exit 0）；E-07（`--with-workspace`）**ERROR 0 / WARNING 0**、exit 0；
  Browser 复核 10/10。日志与产物路径见“整改与复验”节。

## 后续

Phase 10 的 AI 审查整改已在本轮完成并复验；不进入 Phase 11。未完成的是：完整 E-10 仍需
预先约定的多 seed/正式验收；若正式发布要求人类盲测，需要另设 HUMAN gate；语义审查仍只覆盖
20/39 抽样，不能把 AI_REVIEWED 改称 HUMAN PASS。

## 附录：39 个 fixture 的逐条真实 Docker 结论

来源 `artifacts/phase10/fixture-validation/fixtures/*.json`（每条含 base/gold 两次运行的
artifact 路径、逐测试结论、环境身份 `docker:<cache_key>:<image_id>` 与补丁摘要）。
"target" = FAIL_TO_PASS 数；gold 之后通过数列形如 `n/n`；base 列 = 目标测试在 base 上通过的数量。

> 下表是 **2026-10-16 原始记录**，保留历史事实，不是整改后的复验结果。2026-10-18 整改后，
> `budget-category-totals-accumulate-float-error` 的 target 由 2 变为 15、
> `budget-export-adds-currency-column` 由 2 变为 6，其余 8 条仅改 Issue（target/P2P 不变）；
> 全量复验仍为 39/39 VALID，target 91 / P2P 491，见“整改与复验”节与
> `artifacts/phase10/remediation-20261018/e02/summary.md`。

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

