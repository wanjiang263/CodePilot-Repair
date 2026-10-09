# CodePilot-Repair 需求文档（PRD）

> 版本：v1.1  
> 项目定位：Repository-level Autonomous Software Engineering Agent  
> 语言范围：Python（V1）  
> 相关文档：02-实现方案.md（怎么做）、03-AGENTS.md（Coding Agent 的工作规范）、04-分阶段开发任务清单.md（排期与验收）

---

# 1. 项目概述

## 1.1 一句话描述

给定一个 Python 仓库、一个固定 commit 和一份 Issue，CodePilot-Repair 在隔离环境中自动完成定位、修改、测试、失败诊断、迭代修复、回归验证和 Review，输出一个经过执行验证的 patch，以及能完整回放的运行记录。

## 1.2 输入与输出

输入：

- Git 仓库（URL 或本地路径）；
- base commit；
- Issue 文本；
- 可选：环境规格（Python 版本、安装命令、测试命令）。

输出：

- 最终 patch（unified diff）；
- 运行结果：成功 / 失败 / 停止，以及停止原因；
- Run Report：定位结果、修复计划、每一轮的 patch / 测试结果 / 诊断、回归结果、Review 意见、token、费用、耗时；
- 完整 artifact：事件流、LLM 调用记录（可用于回放）、测试原始输出。

## 1.3 核心价值

项目的重点不是“LLM 能写代码”，而是三件事：

1. Repository-aware：利用符号索引、调用图、测试关联和 git 历史缩小搜索范围；
2. Execution-grounded：所有成功判断都来自沙箱中的真实执行，不依赖 LLM 自评；
3. Failure-driven：测试失败后能从结构化的失败信息中诊断原因，有预算、有停止条件地迭代修复。

并且用对照实验回答每个组件是否真的有用。

## 1.4 与现有工作的关系

这个方向已有不少公开工作，本项目不声称提出全新范式。可以参考和对比的代表性工作：

| 工作 | 核心思路 | 本项目的借鉴与差异 |
|---|---|---|
| SWE-agent | 为 LLM 设计专用的 Agent-Computer Interface（文件查看、编辑命令） | 借鉴工具设计；本项目把流程显式化为状态机，而不是单个 ReAct 循环 |
| Agentless | 不用自主 Agent，固定的“定位 → 修复 → 验证”流水线，含复现测试 | 借鉴分层定位和复现测试；本项目加入失败诊断驱动的迭代 |
| AutoCodeRover | 基于 AST 的代码搜索 API + 谱分析定位 | 借鉴结构化检索；本项目做多通道融合并做消融 |
| OpenHands / Moatless 等 | 通用开发 Agent 平台 / 低成本检索式修复 | 作为参考实现 |

具体数字以 SWE-bench 官方榜单（swebench.com）在实验时的数据为准，文档中不预先引用。

本项目可以宣称的贡献，必须由 Phase 12 的消融实验支持，例如：“在同一模型下，结构化失败诊断相比只给原始日志，Resolved Rate 的变化是多少，置信区间是多少”。

## 1.5 要回答的研究问题

| 编号 | 问题 | 对应实验 |
|---|---|---|
| RQ1 | 多通道检索（词法 + 语义 + 结构 + 测试 + 历史）比单一 BM25 的定位准确率高多少 | Phase 2 / 3 检索评估，消融 B vs D |
| RQ2 | 迭代修复相比单次生成，Resolved Rate 提升多少，代价是多少 token | 消融 D vs E |
| RQ3 | 结构化失败诊断相比直接把原始日志交给 LLM，是否提升修复成功率或减少迭代轮数 | 消融 E vs F |
| RQ4 | Agent 自己生成的复现测试有多可靠（Self-verified Precision），是否提升最终成功率 | Phase 6 统计，消融 F vs G |
| RQ5 | 失败的任务主要卡在哪一步（定位 / 理解 / 修改 / 环境 / 预算） | Phase 12 失败案例分析 |

---

# 2. 背景与痛点

1. 仓库规模大，无法把全部代码放进上下文；
2. Issue 描述通常不完整，经常没有 traceback，也没有说明修改位置；
3. 修改位置与 Issue 描述之间有语义鸿沟，正确修改常常不在 Issue 提到的函数里；
4. LLM 第一次生成的 patch 经常有语法错误、导入错误或逻辑错误；
5. 真实测试输出很长，直接塞给 LLM 既贵又容易误导；
6. 局部修改可能破坏其他功能；
7. Agent 容易陷入重复修改、来回振荡和无进展循环；
8. 真实评测中，判分用的测试在修复时是看不到的，Agent 必须自己判断是否修好；
9. 真实仓库的依赖安装和测试环境本身就很容易失败。

---

# 3. 项目范围

## 3.1 V1 必须支持

| 模块 | 内容 |
|---|---|
| Repository | Git clone / checkout 固定 commit；隔离 workspace；Tree-sitter 解析；Symbol Index；Import / Call / Inherit / Test 图；base commit 之前的 git 历史 |
| Retrieval | ripgrep 精确匹配；BM25；embedding 语义检索；图结构扩展；测试邻近；历史；归一化加权融合与 RRF |
| Localization | 规则 + LLM 的 Issue 分析；文件级 → symbol 级分层定位 |
| Agent | Issue Analyzer、Localizer、Impact Analyzer、Planner、Reproduction Agent、Coding Agent、Diagnosis Agent、Patch Reviewer |
| Execution | Docker 沙箱；环境镜像构建与缓存；测试阶段断网；资源限制；junit 结果解析；base commit 测试基线 |
| Repair | 失败解析与分类；失败签名；日志压缩；带证据的诊断；预算；停止规则；增量修改 / 回退重来 / 扩大范围三种策略 |
| Regression | 三级测试；与基线对比的回归判定；flaky 识别 |
| Evaluation | synthetic benchmark；SWE-bench Lite（官方 harness）；指标计算；消融；统计检验 |
| Observability | 节点事件；LLM 调用记录与回放；token / 费用 / 耗时统计 |
| 入口 | CLI（主要）；HTTP API 与 Dashboard（次要） |

## 3.2 V1 不做

- 非 Python 仓库；
- 自动创建或合并真实仓库的 PR；
- 向 Agent 暴露不受限的宿主机 shell；
- 测试执行阶段的网络访问；
- 训练或微调模型、强化学习；
- 分布式执行、多机调度；
- 对抗恶意代码的生产级安全隔离；
- 多用户、权限体系；
- 任意仓库的环境自动推断保证成功（只做尽力推断，失败时要求用户提供环境规格）。

---

# 4. 用户与使用场景

| 角色 | 场景 | 关心什么 |
|---|---|---|
| Developer | 对自己的仓库提交一个 Issue，拿到候选 patch | patch 是否正确、改了哪里、为什么这么改、测试是否通过 |
| Researcher | 在 benchmark 上比较不同配置 | 指标、消融、置信区间、可复现、成本 |
| Reviewer | 审查 Agent 的一次运行 | 每一步的依据、失败如何被诊断、有没有作弊式修改 |

典型用法：

```bash
# 修一个 Issue
codepilot solve --repo https://github.com/org/repo --commit abc123 --issue issue.md

# 跑 benchmark
codepilot bench --suite swebench-lite --subset configs/subsets/test50.txt --config configs/full.yaml

# 回放一次运行
codepilot replay artifacts/runs/<run_id>
```

---

# 5. 功能需求

每条需求都有验收标准，对应的实现细节见 02-实现方案.md。

## FR-01 仓库获取与隔离

- 支持 HTTPS URL 和本地路径；checkout 到指定 commit；
- 每次运行在独立 workspace 中进行，用户原始目录不被修改；
- 验收：运行结束后对原目录执行 `git status`，无任何变化（自动化测试）。

## FR-02 Repository Index

- 记录文件：路径、模块名、大小、hash、行数、是否测试文件、解析错误；
- 识别 symbol：module / class / function / method / test function / test method，含 qualified name、行范围、签名、docstring、装饰器；
- 建立边：IMPORT、CALL、INHERIT、REFERENCE、TEST，每条边带 confidence；
- 有语法错误的文件不中断索引；
- 支持按文件 hash 增量更新；
- 验收：fixture 仓库上的 symbol 和边与人工标注的期望结果一致；在一个真实中型仓库（如 `requests`）上能完成索引。

Symbol 示例：

```json
{
  "name": "login",
  "qualified_name": "auth.service.UserService.login",
  "kind": "method",
  "file": "auth/service.py",
  "start_line": 35,
  "end_line": 67,
  "signature": "def login(self, token: str) -> User"
}
```

## FR-03 Git 历史

- 只读取 base commit 及其祖先，不读取之后的提交；
- 提供文件 / symbol 的近期修改频率；
- 验收：有测试断言 base 之后的提交不会出现在历史特征中。

## FR-04 多通道检索

- 通道：lexical（ripgrep + BM25）、semantic（embedding）、structural（图扩展）、test proximity、git history；
- 每个通道可单独开关；各通道分数先归一化再融合；同时支持加权融合与 RRF；
- 权重可配置；
- 输出 Top-K 候选（函数级和文件级），每个候选保留各通道得分，用于解释；
- 验收：相同输入输出完全一致；能输出各通道单独及融合后的 Recall@1/5/10。

## FR-05 Issue 分析与定位

- 规则抽取 traceback、文件路径、标识符、错误信息；LLM 抽取预期行为、实际行为、关键词；
- traceback 中出现的仓库内位置直接作为最高优先级候选；
- 文件级 → symbol 级两步定位，每一步记录理由；
- 验收：在检索评估集上报告“仅检索”与“检索 + LLM 定位”的 Recall 对比。

## FR-06 影响分析

- 给出目标 symbol 的 callers、callees、相关测试、风险等级（LOW / MEDIUM / HIGH）；
- 风险规则是确定性的，不依赖 LLM；
- 验收：规则有单元测试覆盖每个等级。

## FR-07 修复计划

- Planner 输出根因假设、证据、计划修改（文件 / symbol / 动作 / 说明）、要运行的测试、风险、备选方向；
- Planner 只有只读工具，不能修改文件；
- 验收：Planner 的工具集中不包含任何写操作（测试断言）。

## FR-08 Issue 复现

- 修复前生成一个复现测试，在 base commit 上必须以与 Issue 相关的原因失败；
- 复现失败时降级为使用已有测试，并记录原因；
- 复现测试不进入最终 patch；
- 验收：统计 synthetic 集上的复现成功率和 Self-verified Precision。

## FR-09 Patch 生成

- Coding Agent 通过受控工具读取和编辑代码，主编辑格式为 search/replace 块；
- 编辑匹配失败时返回最相近的片段，让 Agent 修正；
- 每轮工具调用次数有上限；
- 所有路径限制在 workspace 内；
- 验收：工具对路径逃逸（`..`、绝对路径、符号链接）全部拒绝。

## FR-10 Patch 校验

- 校验路径、改动规模、语法、非空；
- 禁止修改已有测试、测试配置和 CI 配置（可按任务显式放开）；
- 检测作弊模式：删除测试、跳过 / xfail、削弱断言、吞异常、探测测试环境；硬编码期望值以启发式标记给 Reviewer；
- 验收：每种作弊模式都有对应的拒绝测试。

## FR-11 沙箱执行

- 所有仓库代码在 Docker 容器内执行，宿主机不执行任何仓库代码；
- 环境构建阶段允许网络并缓存镜像；测试阶段断网；
- 默认限制：2 CPU、4GB 内存、512 进程、300 秒超时、非 root、只读根文件系统、drop 所有 capabilities；
- 测试结果从 junit XML 解析，状态为 PASS / FAIL / TIMEOUT / COLLECTION_ERROR / ENV_ERROR / SANDBOX_ERROR；
- 在 base commit 上生成测试基线；
- 验收：超时、断网、非 root、OOM、无残留容器各有一个 Docker 测试。

## FR-12 失败诊断

- 解析每个失败测试的异常类型、message、traceback，区分仓库内帧和第三方帧；
- 规则优先分类，规则无法判断时才调用 LLM；
- 计算失败签名用于重复检测；
- 压缩日志，控制交给 LLM 的体积；
- 诊断结果必须带证据（`file:line` + 说明），证据中的位置必须真实存在；
- 验收：至少 15 份真实 pytest 失败样本的解析和分类有 golden test。

## FR-13 迭代修复与停止

- 支持增量修改、回退到 base 重新生成、扩大修改范围三种策略；
- 预算：迭代次数、测试运行次数、工具调用次数、token、费用、墙钟时间，全部可配置；
- 停止条件：预算耗尽、重复 patch、连续重复失败、连续无进展、沙箱失败、连续校验失败、诊断放弃；
- 每个停止都有明确的 `stop_reason`；
- 支持中断后从 checkpoint 恢复；
- 验收：每条停止路径有一个图测试；任意输入下图在有限步内结束。

## FR-14 回归验证

- 三级测试：目标 → 影响范围 → 全量；
- 只有“base 上通过、patch 后失败”的测试算回归；
- 新失败重跑确认，结果不一致标记为 flaky；
- 全量测试超时预算时允许抽样，并在报告中标注；
- 验收：“修好目标但破坏别处”“base 本来就有失败”“flaky 测试”三类 fixture 均判定正确。

## FR-15 Patch Review

- Reviewer 只读，评估正确性、改动范围、可维护性、回归风险、是否有不必要修改；
- 测试未通过时不调用 Reviewer；
- 只有高严重度问题会触发一次额外修复，避免无限挑剔；
- 验收：Review 意见出现在 Run Report 中。

## FR-16 评测

- 支持 synthetic benchmark 和 SWE-bench Lite；
- SWE-bench 使用官方 harness 判分；
- 推理与评测信息隔离（见第 7 节）；
- 指标定义见第 8 节；
- 支持消融配置、多 seed、置信区间；
- 验收：指标计算有单元测试；报告中每个数字都能追溯到 artifact 目录。

## FR-17 可观测与回放

- 每个节点记录开始 / 结束事件，包含 run_id、task_id、节点名、时间戳、耗时、token、费用、工具调用、状态、错误；
- 所有 LLM 调用写入 `llm_calls.jsonl`，可以离线回放整个运行；
- 日志和事件中不出现密钥；
- 验收：用回放模式重新执行一次录制好的运行，节点序列和最终 patch 一致。

## FR-18 API 与 Dashboard（次要）

- API：创建任务、查询状态、SSE 事件流、获取 patch / report、取消；
- 默认只监听本机，必须配置 API Key；
- Dashboard 展示：任务、当前节点、时间线、检索候选、计划、diff、测试结果、失败历史、token / 费用 / 耗时、Review；
- 时间不足时可降级为静态 HTML 报告。

---

# 6. 非功能需求

| 编号 | 类别 | 要求 |
|---|---|---|
| NFR-01 | 可复现 | 每次运行保存完整配置、代码版本、LLM 调用记录；可回放。LLM 本身有随机性，统计结论通过多 seed 和置信区间保证 |
| NFR-02 | 可观测 | 每个 Agent 动作都能在事件流中找到 |
| NFR-03 | 可测试 | 核心模块（索引、检索、patch、失败处理、图、评测）行覆盖率 ≥ 80%；Agent 逻辑用 FakeLLM 做确定性测试 |
| NFR-04 | 安全 | 仓库代码只在容器中运行；测试阶段断网；工具路径限制在 workspace；不记录密钥；API 默认仅本机访问并需要认证 |
| NFR-05 | 成本可控 | 单任务费用上限和整批费用上限由用户自行设定，没有固定默认值；token 上限默认 100 万；费用按各模型配置的单价换算为同一币种（默认 CNY）；benchmark 运行前输出费用估算 |
| NFR-09 | 多模型 | 支持 DeepSeek、Kimi、GLM 及其他 OpenAI 兼容接口，通过配置切换，不改代码；不同 Agent 角色可以使用不同模型 |
| NFR-06 | 性能 | 中型仓库（约 1 万个 symbol）首次索引 < 5 分钟，增量索引 < 30 秒；单任务（不含环境构建）中位耗时目标 < 15 分钟。这些是设计目标，实际值以测量为准 |
| NFR-07 | 可扩展 | 解析器、检索通道、LLM provider、sandbox、benchmark 都通过接口接入，便于后续增加语言和数据集 |
| NFR-08 | 平台 | 开发机为 Windows 11 + Docker Desktop（WSL2）；CI 和正式 benchmark 在 Linux 上运行 |

---

# 7. 评测协议与信息隔离

这一节是保证实验结果可信的前提。

推理阶段 Agent 可以使用：

- Issue 文本；
- base commit 的完整仓库内容和已有测试；
- base commit 及之前的 git 历史。

推理阶段 Agent 不能使用：

- gold patch；
- 评测用的测试修改（SWE-bench 的 `test_patch`）和 FAIL_TO_PASS / PASS_TO_PASS 列表；
- base commit 之后的任何提交；
- SWE-bench 的 `hints_text`（默认关闭，可作为单独实验条件开启，并在报告中注明）。

因此 Agent 判断“是否修好”只能依靠自己生成的复现测试和仓库已有测试，这也是复现模块（FR-08）存在的原因。

gold patch 只在推理全部结束后由评测模块使用，用于计算定位 Recall 和判定是否解决。代码上通过把数据拆成“推理任务”和“评测任务”两个对象来保证隔离，并有测试检查推理模块不会引用评测模块。

调参（权重、prompt、预算）只在 synthetic 集和 SWE-bench 开发子集上进行；报告结论使用不重叠的测试子集。

---

# 8. 指标

完整定义和计算方式见 02-实现方案.md「指标定义」。所有比率的分母都是任务总数（含环境失败的任务），环境失败另行报告。

| 类别 | 指标 |
|---|---|
| 定位 | File Recall@1/5/10、Function Recall@1/5/10 |
| 修复 | Resolved Rate、First Attempt Resolved、Repair Gain、Repair Success Rate |
| 可靠性 | Self-verified Precision（Agent 自认为成功时实际成功的比例）、Empty / Invalid Patch Rate |
| 过程 | 迭代次数（均值 / 中位数 / P90）、stop_reason 分布 |
| 成本 | 每任务 token、费用、耗时；每解决一个任务的平均费用 |

v1.0 中的“Regression-free Rate”合并进 Resolved Rate：SWE-bench 的判定本身就要求 PASS_TO_PASS 测试不失败。

---

# 9. 验收标准

V1 完成需要同时满足以下各项。性能指标不设预期值，只要求真实测量并如实报告。

| 编号 | 标准 | 验证方式 |
|---|---|---|
| A | 能获取仓库并在隔离 workspace 中工作，原目录不变 | 自动化测试 |
| B | 能在一个真实中型仓库上完成索引 | 命令输出 + 耗时记录 |
| C | 能输出各检索通道及融合后的 Recall@K | 检索评估命令 |
| D | 能在 Docker 中构建环境、运行测试、生成基线 | Docker 测试 |
| E | 能生成复现测试并验证其在 base 上失败 | synthetic 集统计 |
| F | 能生成、校验、应用 patch，并拒绝全部已定义的作弊模式 | 单元测试 |
| G | 能解析并分类真实 pytest 失败，生成带证据的诊断 | golden test |
| H | 至少有一个任务在真实 LLM 运行中经历“第一轮失败 → 诊断 → 后续轮次通过” | Run Report |
| I | 所有停止路径都能正确触发并给出 stop_reason | 图测试 |
| J | 回归判定在三类特殊 fixture 上正确 | 集成测试 |
| K | 至少 30 个通过校验的 synthetic fixture，覆盖 10 个类别 | fixture 校验脚本 |
| L | 在 SWE-bench Lite 的固定 50 题测试子集上完成推理，并通过官方 harness 评测 | 评测日志 |
| M | 完成至少 A / D / G 三组消融，报告包含置信区间与失败分析 | 实验报告 |
| N | 任意一次运行都能通过 artifact 回放 | 回放测试 |

---

# 10. 交付物

```text
codepilot-repair/
├── 源码（src/codepilot）与 CLI
├── Docker 沙箱镜像定义
├── 测试（unit / integration / graph / docker / e2e）
├── synthetic benchmark（≥30 个 fixture）与校验脚本
├── SWE-bench Lite 推理与评测脚本、固定子集列表
├── 消融配置与结果（results.csv、metrics.json）
├── 实验报告（含失败案例分析、成本、局限性）
├── 架构文档与 API 文档
├── API + Dashboard（或静态 HTML 报告）
└── Demo 录屏
```

---

# 11. 成功标准

项目成功的标志不是“Agent 能写代码”，也不是界面好看，而是：

> 在真实仓库和真实 Issue 上，Agent 能定位问题、提出修改、在隔离环境中执行验证、从测试失败中恢复、通过回归检查，整个过程可审计、可回放；并且有对照实验说明每个组件带来了多少收益、花了多少成本。

所有指标以实际实验结果为准，不预设、不伪造。实验结果不理想时如实报告，并通过失败分析说明原因，这同样是有效的交付。

---

# 12. 风险与假设

| 风险 / 假设 | 说明 |
|---|---|
| 环境构建 | 真实仓库的依赖安装是最大的不稳定因素。SWE-bench 复用官方环境逻辑；用户仓库需要提供环境规格 |
| 复现测试质量 | 如果复现测试写错，Agent 可能把错误的行为当成目标。通过 Self-verified Precision 监控 |
| 成本 | SWE-bench 消融需要多组配置 × 多题 × 多 seed，运行前必须估算并设定总预算 |
| 数据污染 | 主流模型可能在训练中见过 SWE-bench 相关仓库和修复，报告中需要说明，synthetic 集作为对照 |
| 统计功效 | 50 题规模只能检测较大的差异，小差异需要更大样本或明确说明不确定 |
| 单人开发 | 总工作量约 13–14 周，进度落后时按 04 的降级顺序裁剪，核心闭环不裁剪 |

