# Phase 10 AI 语义审查索引（2026-10-18）

本文件只保存可追溯索引与结论，不包含 Gold Patch、隐藏测试正文或模型原始响应。
审查主体为 Codex AI，不是人工签字。完整只读材料、逐项五维判断与 Browser Use 页面位于
Git 忽略路径 `artifacts/phase10/ai-semantic-review-20261018/`。

抽样规则：`category ASC, fixture ID ASC`，每类前 2 条；20/39。内容哈希覆盖 metadata、Issue、
Gold Patch、隐藏测试定义/补丁与 metadata 声明的全部 repo 文件，按稳定路径顺序计算 SHA-256。

| # | fixture | category | content SHA-256 | severity | recommendation |
|---:|---|---|---|---|---|
| 01 | `budget-render-row-ignores-column-order` | `api_misuse` | `ccaec21ff2b4c63421a17e07c1e519ec12321dd2450536170e7a564c09e09276` | P2 | REVISE |
| 02 | `inventory-describe-crashes-on-unknown-sku` | `api_misuse` | `c476c683cc579f9d7bcd2481cc6496cda40634af8c61a0de312f456949cb0a34` | P2 | REVISE |
| 03 | `budget-export-adds-currency-column` | `cross_module_dependency` | `a8b235aa8cf28f92606cc70f2ab6ed0cfaf998beeb03da0c1cab8853eeea1cb6` | P1 | REVISE |
| 04 | `httpclient-config-snapshot-loses-retries` | `cross_module_dependency` | `0a7ec1c98c1ec4fc29b30ab01d6ce1bd4fa74946faaacf5aa750c6574d618d70` | P2 | REVISE |
| 05 | `budget-category-totals-accumulate-float-error` | `edge_case` | `7da7079e86b102b49f2da15a12563dab1da3034bd85d6ef451580b97831a3e5b` | P1 | REVISE |
| 06 | `httpclient-zero-retries-makes-no-request` | `edge_case` | `8360e7b65aeaa99f245f466adc3b8addae0d86b1c8ef706c0494d39e34479628` | none | KEEP |
| 07 | `budget-refund-records-a-charge` | `incorrect_return` | `6cbfea43c3f2a639562993d7344552eaf8d92032fae09cd489712c4e1b8b5bb6` | P2 | REVISE |
| 08 | `taskqueue-pop-takes-from-the-wrong-end` | `incorrect_return` | `bb59bbc75e6b40692f8dc6aec6dd4890b9992cad5728455bece054e96ccbc259` | none | KEEP |
| 09 | `budget-ledger-add-accepts-nonpositive` | `missing_validation` | `117487174f84fa659fa810815bffe508cbe1028088420acbc0b6cbd99248964c` | none | KEEP |
| 10 | `budget-ledger-refund-accepts-nonpositive` | `missing_validation` | `a156b59f17aecabaaa3708b83bd6b9c8025668cb2497fadc1ac6eb928467739f` | none | KEEP |
| 11 | `taskqueue-retry-makes-one-fewer-attempt` | `off_by_one` | `2fd46d58ac701802507169dc8e208bcbcdfc4b93b950b364310e8def55fb2f33` | P2 | REVISE |
| 12 | `textkit-tail-eats-one-extra-char` | `off_by_one` | `43c6cdb188abc9cf2611e87f539c05abf4ba6968796521139cc8bed41106638d` | none | KEEP |
| 13 | `budget-categorise-loses-the-rent-branch` | `regression_bug` | `aa63ccf4e8cc8c863c2f25bf5acd43e5ea38719ca053b67244ea4e5cd4fbb89f` | P2 | REVISE |
| 14 | `httpclient-config-stops-checking-timeout` | `regression_bug` | `224f61219823f26dc9740b3e31cc7e02517862e27aa4dfaae3ba5ae208a395b3` | P2 | REVISE |
| 15 | `budget-snapshot-shares-the-live-list` | `state_mutation_bug` | `9a4eee17c9aba54f58577db92e2cb16bd0c2117bf6877d3d4cd25867c463a4de` | none | KEEP |
| 16 | `httpclient-recorder-aliases-the-caller-list` | `state_mutation_bug` | `a4f21d9eceb5ed147a37d230a9d5d2a582acc26f853b1a099adcc3d46d6f3329` | none | KEEP |
| 17 | `budget-food-branch-drops-lunch` | `wrong_condition` | `71f61ff92f6a3787d6bf723b45597025b2bb0f854803f34ee4949470a8a23975` | none | KEEP |
| 18 | `budget-member-discount-inverted-condition` | `wrong_condition` | `6ba2a78a475ed3498bb50b0034a04f11e61347070baa26be6a712cd73925fa1a` | P2 | REVISE |
| 19 | `httpclient-retries-rejects-with-wrong-error` | `wrong_exception` | `46f2b1ccf7de3c28c7751d838a74f2d2410edcc4447139fb3402583c535e6667` | none | KEEP |
| 20 | `inventory-reserve-raises-with-wrong-error` | `wrong_exception` | `fdf3849b532f1b53743e9285155de6c9136a9bcd659bd58c5bba857f14d661a4` | none | KEEP |

汇总（审查轮）：KEEP 10 / REVISE 10 / REJECT 0；P1 2 / P2 8 / none 10。八条 REVISE 为改写式
语义提示；另两条分别为 currency 来源契约歧义、float 逐步舍入 Gold/测试边界不足。全部外部
真实性问题标记 `UNVERIFIED`。该审查轮未修改 fixture、Gold Patch 或隐藏测试；同日随后的整改与
复验见下节。

## 2026-10-18 整改与复验（同日后续，仍是 AI 复核）

10 条 REVISE 已全部整改并复验：8 条 Issue 改为纯可观察行为/必要契约；currency 采用
`Ledger.currency`（默认 CNY，本轮新增**合成契约**）并重写 Gold/测试；precision 明确“十进制
精确累加、每类别最终只舍入一次到两位、半分取偶、顺序无关、类别独立”并重写 Gold/测试。
只改权威来源 `scripts/synthetic_tasks.py` / `scripts/synthetic_hosts.py`，仅重建 10 个 fixture，
另 29 个逐文件未变。

| fixture | 原 severity | 整改后 content SHA-256 | 复验结论 |
|---|---|---|---|
| `budget-render-row-ignores-column-order` | P2 | `fc65ab68f687e801c62a39241737a5a848190230fc623d9ec1f52f42c83f29c7` | LEAK REMOVED / CONSISTENT |
| `inventory-describe-crashes-on-unknown-sku` | P2 | `ed0c2962880006ef990b7e8c13fbbe8846af83668ee894116c085bc722ed18e3` | LEAK REMOVED / CONSISTENT |
| `budget-export-adds-currency-column` | P1 | `8038f15366e85f9821eb533543572f49fbbc15acb490ca7d538cb7bdbe3d82fc` | CONTRACT CLARIFIED / GOLD+TESTS UPDATED |
| `httpclient-config-snapshot-loses-retries` | P2 | `c214f049486461a6e3694cb2074503ccb10fddb0d6ad24a6fe21a0a3bc2caefb` | LEAK REMOVED / CONSISTENT |
| `budget-category-totals-accumulate-float-error` | P1 | `8b38c98621fdb05771f05c2d1c80db0c5a5865fbbf24c19653268cab2e685f88` | CONTRACT CLARIFIED / GOLD REWRITTEN / TESTS STRENGTHENED |
| `budget-refund-records-a-charge` | P2 | `d9b94055f66f2612ff716bd5f97b5ade515d6a5842f3f3eee5e525e405e9190b` | LEAK REMOVED / CONSISTENT |
| `budget-categorise-loses-the-rent-branch` | P2 | `ce389ca5dd25060de51b91b51aadb55896e2495c3aa6a3e807b329949fc81bf9` | LEAK REMOVED / CONSISTENT |
| `budget-member-discount-inverted-condition` | P2 | `00f93879bbc08bd775a0ed41294ee377e1700aa1f9de33bfa412bd8476306723` | LEAK REMOVED / CONSISTENT |
| `httpclient-config-stops-checking-timeout` | P2 | `916c7ed7d5dda8081773fe04ef9f99056dbb34875cdbdc0eb2b3a0bd2a874323` | LEAK REMOVED / CONSISTENT |
| `taskqueue-retry-makes-one-fewer-attempt` | P2 | `3d8ae3b9198de1b57eed065c1749fa3ad9035b3e8bdef19f83ee1ab81d70ade8` | LEAK REMOVED / CONSISTENT |

自动复验（独立目录，未覆盖原记录）：

- E-02 真实 Docker：**39/39 VALID**；无效 0、环境错误 0、续跑复用 0；base 目标失败 39、
  gold 目标通过 39、无新增回归 39；target **91**（原 74；precision 2→15、currency 2→6）、
  P2P 491；909.87s；`artifacts/phase10/remediation-20261018/e02/summary.json`。
- E-07 含 `--with-workspace`：**checked 39 / ERROR 0 / WARNING 0 / exit 0**；
  `artifacts/phase10/remediation-20261018/e07.json`。
- Browser 逐页复核：10/10、HTTP 200、每页 14～15 个材料区块；截图、页面 innerText 与 SHA-256、
  console/page error 在 `artifacts/phase10/remediation-20261018/browser-review/`；逐样本判断见
  `artifacts/phase10/remediation-20261018/review.md`。
- 数据一致性：`consistency.json`（39 条记录，10 改动 / 29 未变 / 673 个历史文件哈希未变，
  含整改前后哈希、fixture_version、base commit、gold/test patch SHA-256、原/新 E-02 引用）。
- 定向测试：`pytest tests/eval/test_synthetic.py tests/eval/test_leakage.py -q` = **64 passed /
  3 skipped**（符号链接环境限制），日志与退出码在同目录 `targeted-tests.log` / `.exit`。
  一次落盘重跑曾遇到宿主临时目录 `WinError 5`（pytest 在 workspace 临时树内 `os.replace` 被拒），
  失败日志原样保留为 `targeted-tests.failed-20261018T2349.log`，诊断见
  `targeted-tests-diagnosis.md`；原样重跑通过，未删除测试、未新增 skip、未降低断言。

残余限制：这仍是 AI 复核，不是人工盲测或业务方验收；语义审查只覆盖 20/39 抽样，本轮只重审被
修改的 10 条；两个 P1 契约是本轮明确的合成契约，不是外部业务规则；E-07 的 0/0 只覆盖字面/
结构规则；E-10 仍为 **PARTIAL / ACCEPTANCE INCOMPLETE**。公开文档继续禁止使用“已证明无泄漏”。
同日另做独立 AI 对抗复核（`independent-audit.md`）：10/10 无 fixture 级缺陷，另记 F1–F4
低危覆盖缺口（F1 timeout 下界最接近评分影响）与 O1/O2/O4 观察；超出本轮范围，留作后续补测。
