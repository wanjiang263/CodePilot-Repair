# Phase 2 最终审查 P1：索引失败传播

本轮基于 `codex/phase2-retrieval-eval` 的 `a097005`，仅修复索引不完整被报告为
正常评估的问题。Structural `04132b8` 与 Fusion `9688388` 不变，未 Push、未操作
PR #4 或其他 PR，未进入 Phase 3。

## 根因与处理

`retrieval/runner.py` 原先丢弃 `Indexer.index()` 的 `IndexStats`。扫描器会把不可读
路径记录到 `ScanResult.unreadable`，不抛出异常，因此非 Gold 文件读取失败也能产出
部分索引并被评估当作成功。

Runner 复用真实扫描器，一次扫描保留相对路径，并检查返回统计。`unreadable > 0`
或索引后端执行异常时，抛出有类型、阶段、错误代码、数量和路径的 `IndexingError`，
在事务回滚后清理临时工作区，不把部分索引交给任一通道。空文件扫描结果也按
`is not None` 判断，保留不可读根目录 `.`，避免 `ScanResult.__len__` 的假值误判。

既有 ignored / too_large / binary / symlinks 策略和 R-05 容错解析语义不变，非致命
诊断通过可选 `ExecutionInfo.index_diagnostics` 记录。成功且无诊断时不增加 JSON
字段；冻结的 `RetrievalResult`、检索配置和 CLI 参数均未修改。

`retrieval/evaluation.py` 保留既有 `failures`，补充可选 `failure_details`。失败字段
区分 environment / execution、workspace / index / retrieval、原异常类型、原因、
数量与扫描计数。只在检索执行边界捕获执行异常；Gold 校验仍在推理之后，非法 Gold
仍显式终止，未变成检索失败或检索提示。`RUN` 仍表示实际尝试，失败与 `NOT RUN`
独立记录。正常零候选留在 `results` 中，没有失败；环境失败没有正常候选记录。

指标仍以全部任务为分母：失败贡献零命中，并另报失败任务数、原因和缺失预测。
文本表附带稳定排序的失败原因。`eval-retrieval` 先保存完整 JSON / 打印表格，存在
失败任务则 exit 1；无失败（包括合法零候选）则 exit 0。旧 / 新单通道及多通道
`retrieve` 都拒绝不完整索引并 exit 1。

## 回归与复现

新增 `test_retrieval_index_failures.py` 的 18 项测试。原始 17 项在修复前
**15 failed / 2 passed，exit 1**；额外的空扫描不可读根目录用例先证明路径丢失
（**1 failed，exit 1**）再修复；最终 **18 passed，exit 0**。第一次实现后
**13 passed / 4 failed**，4 项失败来自同进程 CliRunner 继承 INFO 缓存 logger。
测试 fixture 重新建立 ERROR logger，使其与新 CLI 进程一致；未放宽字节一致性
断言，也未改生产日志模块或任何既有测试。

首轮全量为 **1142 passed / 5 failed / 1 skipped / 1 deselected，95.04%，exit 1**：
新增 fixture 遗留全局 ERROR 级别，导致既有缓存 WARNING 捕获断言失败。修正
fixture 的 teardown，完整还原之前的 structlog 配置、根 logger 级别及捕获
handlers；按新 fixture → 既有缓存测试顺序验证 **16 passed，exit 0**，不改
任何既有断言。首轮完整日志 / XML / 覆盖率保留，随后重新执行最终全部门禁。

重放原审查脚本，保留其内容与原始前证据不变。原脚本断言“所有行失败数为零”
现在触发预期 AssertionError；验证器确认真实扫描仍为 **files=13 / unreadable=1**，
七行各 **1 个失败、0 个正常结果、分母 1、零命中**。这不是把原脚本报告为 PASS：
通过的是验证旧问题断言不再成立的修复后验证器。

docs/04 两条原样 CLI 均 exit 0。全部 12 条正常样例的完整评估 JSON 与原始 gzip
解压后的字节完全相同（SHA-256：
`cdc5d81253cbd0c389275d0dd54c221a462495ab11ff9d49404d832fa301f957`），七行
失败数均为零，raw / normalized / fused 分数、排名、Gold 口径及 Recall / Exact
均未变化。普通 `retrieve` 的原始 stdout 也与旧证据逐字节一致。

完整定向验证、独立 CLI 故障验证和最终质量门禁结果见
[本轮证据索引](../evidence/phase2-index-failure-20261011/README.md)。

最终完整离线门禁：ruff check / format check / mypy src / pytest 均 **exit 0**；
pytest **1147 passed / 0 failed / 1 skipped / 1 deselected**，含分支覆盖率
**95.04%**（上一轮 1129 passed / 94.91%）。唯一 skip 为未配置预安装语义模型；
Docker 标记用例被排除，不计作 PASS。P1 已在本地消除，具备获得明确授权后
Push / 创建 Stacked PR 的本地准备条件；Retrieval Eval 的建议 base 仍为
`codex/phase2-fusion`。本轮仅追加一个 `fix(retrieval)` 提交，不执行发布。

真实模型 / Embedding API / GPU / 产品 Docker Sandbox / LLM / 远端 CI / 正式真实
问题基准 NOT RUN；Semantic 仍为明确标识的 offline-hash 非学习基线。
L-18 保持独立待办，真实验收及预算授权后再考虑 Phase 3。

## 发布轮补充：Linux CI 的一次跨平台断言修正

获授权 Push 并创建 Stacked PR 后，远端 CI 首次实际运行本分支（PR #6）。
`lint + types`、`Phase 1 index in Docker` 通过；Linux pytest 只有
`test_complete_index_success_retains_prior_raw_candidates_and_execution_json`
失败（`1 failed / 1141 passed / 6 skipped / 1 deselected`），Windows 本地为全绿。

失败差异共 174 行，逐行核对全部是 40 位 Symbol ID（`symbol_id` 字段与文件级
`top_symbols` 列表），分数、归一化、rank、weight、排序、`reason`、query 解析与
`execution` 均无一行不同。根因是既有设计而非本轮回归：`symbol_id =
sha1(repository_id + qualified_name + kind)`，`repository_id` 又哈希源仓库的绝对
路径（`workspace.py` 的 `repository_id()`），所以 Windows 生成的证据 ID 不可能与
Linux checkout 相同。断言直接比较整份结果 JSON 因此把机器路径当成了验收条件。

最小修正只在该测试内把 Symbol ID 绑定到它必须编码的身份
（`qualified_name|kind|file`），再比较两份完整结果：ID↔身份冲突会在收集阶段报错，
其余字段仍逐字节等价比较，排序与解释没有放宽。既有 18 项测试仍全部通过，
`ruff check` / `format --check`（163 files）/ `mypy src`（87 files）exit 0。
Linux 与 Windows 的用例总数同为 1148（本地 1147 passed + 1 skipped，
CI 1142 passed + 6 skipped），差异只是平台门控用例，非本轮引入。

真实模型 / Embedding API / GPU / 产品 Docker Sandbox / LLM / 正式真实问题基准
仍为 NOT RUN；Phase 2 的远端工程验收以 PR 的 CI 结果为准。
