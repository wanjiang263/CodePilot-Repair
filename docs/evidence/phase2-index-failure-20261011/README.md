# Phase 2 最终审查 P1 索引失败修复证据

基线：`codex/phase2-retrieval-eval` / `a097005`；仅本地最小修复。
说明见 [修复记录](../../notes/phase2-index-failure.md)。

## 实际执行记录

| 检查 | 真实结果 | 证据 |
|---|---|---|
| 修复前回归 | 15 failed / 2 passed，exit 1 | `red.log/json/xml` |
| 首次实现后回归 | 4 failed / 13 passed，exit 1；CliRunner 缓存 INFO logger 导致 stderr 不确定，测试隔离修正，未放宽断言 | `green.log/json/xml` |
| 空扫描不可读根目录，修复前 | 1 failed，exit 1；`.` 路径被假值判断丢失 | `root-red.log/json/xml` |
| 新增回归，最终 | 18 passed，exit 0 | `green-final.log/json/xml` |
| V-21～V-25 与新增回归 | 80 passed，exit 0；含真实 Git / 独立 CLI 完整字节确定性 | `targeted.log/json/xml` |
| Ruff check | PASS，exit 0 | `ruff-check.log/json` |
| Ruff format check | PASS，163 files，exit 0 | `ruff-format.log/json` |
| mypy src | PASS，87 files，exit 0 | `mypy.log/json` |
| 首轮完整离线 pytest | 5 failed / 1142 passed / 1 skipped / 1 deselected，95.04%，exit 1；新增 fixture 未还原全局 ERROR 级别，影响既有 WARNING 断言 | `pytest-full-first.log/json/xml`、`coverage-first.json` |
| 日志状态还原回归 | 16 passed，exit 0；先执行新 fixture，再执行全部既有缓存回归，未改断言 | `logging-isolation.log/json/xml` |
| 最终完整离线 pytest / 覆盖率 | 1147 passed / 0 failed / 1 skipped / 1 deselected，95.04%（含分支），exit 0 | `pytest-full.log/json/xml`、`coverage.json` |
| docs/04 原样 retrieve / eval CLI | exit 0 / 0 | `cli-retrieve.*`、`cli-eval.*` |
| 正常 12 条样例对比 | 完整报告与旧 gzip 原始字节相同；全部七行失败数 0；retrieve stdout 同字节 | `normal-comparison.json`、`normal-verification.log/json` |
| 原 P1 脚本重放 | 旧“失败数零”断言失败，修复后验证器 exit 0；七行各失败 1、无正常结果，分母 1 | `reproduction-before.json`、`reproduction-after.json`、`reproduction-verification.json`、`reproduction-final.log/json` |
| 独立进程故障 CLI | legacy / single / multi / eval 各运行两次，每次 exit 1；stdout / stderr / 失败报告字节一致 | `failure-*.stdout/stderr/json`、`failure-evaluation.json`、`failure-cli-verification.json`、`failure-cli.log/json` |

`run-check.py` 保留真实子命令退出码，并保存实际日志，不把失败变成 PASS。
`run-cli.py` 保留原始 stdout / stderr；原样 docs/04 命令只额外设置
`UV_OFFLINE=1 UV_NO_SYNC=1 PYTHONIOENCODING=utf-8`，使用已有环境，不安装依赖。
INFO 诊断日志含时间，单独保存；完整 CLI 确定性测试显式使用 ERROR 级别，比较
原始字节，不清洗时间、路径、排名或分数。

`verify-reproduction.py` 重放原脚本，检查其旧断言的实际失败位置，未修改原脚本
或原先 `artifacts/phase2-final-review-20261011-a097005/` 的前证据。第一次重放与
最终重放日志均保留；最终机器摘要为 `reproduction-verification.json`。
`verify-failure-cli.py` 仅 Mock 非 Gold 文件读取的 PermissionError；扫描器、索引、
工作区、CLI 和评估器均实际执行，没有 Fake 检索结果。

正常完整报告 SHA-256 为
`cdc5d81253cbd0c389275d0dd54c221a462495ab11ff9d49404d832fa301f957`。
为避免重复保存相同报告，引用
[原始完整 gzip](../phase2-retrieval-eval-20261011/retrieval-evaluation.json.gz)。
`verify-normal.py` 对解压字节与新报告直接比较，保存全部 Recall / Exact 指标。

## 复现与边界

```powershell
.venv/Scripts/python.exe docs/evidence/phase2-index-failure-20261011/verify-reproduction.py
.venv/Scripts/python.exe docs/evidence/phase2-index-failure-20261011/verify-failure-cli.py
```

失败任务独立记录 `failure_details` 的 stage、category、code、exception_type、
message、count、paths、scan_counts；七行共用失败索引，无通道读取部分索引。
任务失败与正常空候选明确分开，任务总数分母不变。非致命扫描策略和容错解析
不一概判失败；无诊断时省略新增字段以保持正常成功 JSON 兼容。

12 条样例仍是合成开发数据，Semantic 是 offline-hash 非学习基线。真实 LLM / L-18、
真实语义模型、Embedding API、GPU、产品 Docker Sandbox、远端 CI、正式真实问题
基准 **NOT RUN**。本轮没有 Push、PR 操作或 Phase 3 开发。

最终结构化汇总及文件哈希清单由 `summarize.py` 从实际记录生成，不替代原始日志。
`acceptance.json` 对照上一轮 JUnit：新增恰好 18 项，没有删改已有测试 ID；
覆盖率由原先 94.91% 变为 95.04%。唯一 skip 是未设置预安装模型路径，Docker
标记用例被离线选择排除，均不计作 PASS。完整 P1 复现与本地门禁已通过，满足
获得明确发布授权后的本地 Push / Stacked PR 准备条件；远端 CI 仍 NOT RUN。
