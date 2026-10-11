# Phase 2 第四批：V-14～V-20（通道配置、融合与文件聚合）

2026-10-11。本批从已存在且干净的 `04132b8` 创建本地分支 `codex/phase2-fusion`，仅做本地提交。`codex/phase2-structural-retrieval` 始终指向 `04132b829fca2ef2ec6296092052100e0068eca8`；没有 Push，也没有创建、更新、合并或关闭 PR。后续获授权创建 Stacked PR 时，base 应为 `codex/phase2-structural-retrieval`。

## 接口与算法决定

- V-14：`RetrievalConfig` 为冻结的 Pydantic 配置，六个布尔开关默认仅 BM25 为 True。配置调用预检 semantic 的 provider、结构 / 测试通道的 graph、history 的 churn、ripgrep 的可执行文件。Structural 只能使用显式 seeds 或已启用的种子通道，避免暗中运行已禁用通道。
- V-15：`min_max()` 按 Symbol ID 输出确定性字典；空输入为空，全等分数全部 1.0。仅在 `hi-lo` 溢出时缩放，保留普通 / 负数 / subnormal / 相邻浮点的精度。非有限输入明确报错。
- V-16：`weighted_fusion()` 先逐通道归一化，再以有限非负权重加权；缺权重、缺候选按 0，不重新归一化用户权重。默认仍是 semantic .30 / lexical .20 / structural .20 / test .15 / history .15。服务暴露六个独立通道，所以结果层在已启用的 BM25、Ripgrep 之间平分 lexical 预算：两者启用各 .10，只有一个启用则 .20；空但已启用的通道仍占其配置权重，不把缺证据变成其他通道奖励。也可显式提供 `bm25` / `ripgrep` 权重；与 `lexical` 混配时明确报错以防重复计权。这是初始工程默认，不是已验证的科学最优权重。
- V-17：`rrf(k=60)` 每个通道去重后赋 1-based 排名，按 `1/(k+rank)` 累加。通道顺序不影响分数；纯 ID 函数同分按 ID，完整候选同分按 file / start_line / qualified_name / symbol_id。`k=0` 也有定义，负数 / 非整数 / bool / 不可表示参数拒绝。
- V-18：只读 `in_repo=True` 且同时匹配文件、函数名、行范围的帧，多个嵌套命中选择最窄 Symbol；`<module>` 可映射索引中的模块。重复帧 / 理由去重并排序。Pinned Symbol 从归一化及 RRF 排名输入移除，融合分 0，按业务键排在普通候选之前。原始通道结果仍保留；因此 `per_channel` 的 normalized 描述完整通道候选集，融合候选的 normalized 描述去掉 pins 后实际参与融合的候选集。
- V-19：同一 Symbol 只计一次；文件分严格为 `max + 0.1 * 其余最多三个最高分`。同 ID 的矛盾身份拒绝，正常重复取最强。文件排序为 pinned 文件优先、分数降序、path 升序，没有 pin 数值奖励。`top_symbols` 存 Symbol ID，包含参与公式的 Top-4 和全部 pins，pins 在前；reason 保存公式及置顶理由。
- V-20：`build_result()` 产生完整 `per_channel`、完整融合排名和 Top-K Symbol / File 视图。普通候选记录每个启用通道的 raw、实际 normalized、rank、weight（加权时），缺证据明确为 0。`validate_result()` 对反序列化结果复算分数、排序、解释、归一化与文件聚合；这是算术及内部一致性检查，仓库归属由构造前的 parser / index 确认。

向后兼容扩展只有可选默认字段：`RankedFile.pinned=False`、`RetrievalResult.fused_symbols=[]` 和 `RetrievalResult.rrf_k=60`。`fused_symbols` 防止 Top-K 丢失完整融合解释；`rrf_k` 让非默认 RRF 快照可独立复算。旧 JSON 可省略这些字段，旧类型和参数仍可使用。`snapshot_id` 保持默认空字符串，可原样携带调用方引用，不生成 ID、不写持久化快照；落盘引用解析属于后续 C-11 集成。

`retrieve_symbols` 只增加可选参数。原单通道调用与只有一个启用通道的默认配置仍返回原始排名、`fusion_method=none`、空 files / weights / fused_symbols；显式 fusion_method 或权重才启用单通道融合。多通道配置默认加权融合，也可显式 RRF。Top-K 在完整融合 / 聚合之后截断，不改变分数或原始候选集。CLI、R-17 三个图查询及 V-01～V-13 通道算法没有改动。

## 逐项提交与测试

| 任务 | Commit | 新增测试 | 逐项实测 |
|---|---|---|---|
| V-14 | `3af52b7` | `test_channel_config.py` 21 项 | 37 passed（包括单通道 / 既有三通道集成） |
| V-15 | `327c16d` | `test_normalization.py` 15 项 | 26 passed（包括词法回归） |
| V-16 | `126df8d` | `test_weighted_fusion.py` 14 项 | 29 passed |
| V-17 | `4cff042` | `test_rrf.py` 13 项 | 42 passed |
| V-18 | `7d3a0f1` | `test_pinning.py` 13 项 | 55 passed |
| V-19 | `9d43fc8` | `test_aggregate.py` 15 项 | 28 passed |
| V-20 | `652028a` | `test_result.py` 28 项 + `test_retrieval_fusion.py` 12 项 | 339 passed / 1 skipped（全部检索、Schema、旧 CLI 回归） |

新增共 **131 个 pytest 用例**。每项先记录未实现模块 / 函数的失败，再实现并跑通过；V-20 还保留非默认 k 和服务参数的失败到修复记录。V-14 测试最初名为 `test_config.py`，全量收集发现与 LLM 既有模块同名后仅把新文件改名，断言与 pytest 配置保持原样。

最终全量质量门禁与覆盖率统计见 [验收证据](../evidence/phase2-fusion-20261011/README.md) 及机器可读 `acceptance.json`：

| 最终门禁（按顺序执行） | 实测结果 |
|---|---|
| `ruff check .` | PASS，exit 0 |
| `ruff format --check .` | PASS，151 files already formatted，exit 0 |
| `mypy src` | PASS，81 source files，exit 0 |
| `pytest -m "not docker and not llm" --cov --cov-report=term-missing` | PASS，**1067 passed / 1 skipped / 1 deselected / 0 failed / 0 errors**，exit 0；终端计时 320.93s，JUnit suite 317.671s |
| 语句 + 分支综合覆盖率 | **95.12%**（语句 4825/5011，分支 1201/1324），80% 门禁保持原样 |
| 新模块 / service 综合覆盖率 | config 100%、fusion 98.08%、aggregate 100%、result 98.31%、service 100% |
| 六通道加权 / RRF、连续两次 UTF-8 JSON 字节一致、输入顺序扰动、JSON round-trip 与一致性复算 | PASS，见 `final-pytest.xml` 的 `test_retrieval_fusion` / `test_result` 用例 |
| 既有单通道、CLI、通道及 R-17 图查询回归 | PASS，完整 pytest 包含；相对 `04132b8` 的冻结模块 diff 为空 |

唯一 skip 是没有配置真实本地 code embedding model；唯一 deselected 是 Docker 标记测试。改名修复独立提交为 `f084a16`，不改测试断言、依赖、门禁或既有测试文件。

## 实际遇到的问题

1. 默认沙箱禁止 `.git` 写入，且无法访问 pytest 既有临时目录。已通过权限机制执行用户授权的本地分支 / Commit / 离线测试；`v14.log` / `.xml` 保留环境导致的 13 passed / 24 errors，放行后 `v14-green` 为 37 passed。
2. 最初日志编码遇到 Windows GBK，记录器显式使用 UTF-8 后重新记录 red 结果，所有记录的子命令真实退出码保存在同名 JSON，不使用外层 shell 的最后成功命令覆盖失败。
3. V-20 最初夹具只有三个文档，BM25 种子不为正，Structural 按冻结规则不扩展。添加无关文档制造正 IDF；没有改断言或通道算法。`v20-first` 保留 53 passed / 2 failed。
4. 非默认 k 原本未写进快照，单通道显式权重 / snapshot_id 和无 RRF 模式的非默认 k 会被忽略。失败测试暴露后补齐参数记录 / 接线 / 明确错误；`v20-k-red` 与 `v20-options-red` 保留真实失败。
5. 首轮完整 pytest 因同名 `test_config` 在收集阶段终止，`full-pytest` 保留 1 error / 1 deselected，未把它视为全量通过；改名后按四项门禁重新验收。

## 未完成 / NOT RUN

- V-21～V-25 仍未完成：Recall@K 评估器、≥10 gold location 样例、多通道 CLI、评估 CLI 和依赖该 CLI 的整阶段确定性验收。本轮只验证 Python 服务及结果层的重复运行逐字节一致，不能代替 V-25。
- 04 Phase 2 的完整 Recall@K 表格及多通道 CLI 验收 NOT RUN，Phase 2 整阶段不宣称完成；没有运行 Recall@K，没有定位准确率提升结论。
- Docker / 真实本地 Embedding / 真实 Embedding API / 真实 LLM / Linux CI 均 NOT RUN：本轮只执行离线本地门禁，范围不涉及 sandbox，无 Push；已有 L-18 真实验收仍为遗留项。
- 六通道集成使用真实 BM25 / Ripgrep / Structural / Test / History / Semantic 实现；边界输入分别是内存 FakeGraph、手工 FileChurn 和确定性 FakeEmbedding，不是实际模型推理或检索质量实验。
