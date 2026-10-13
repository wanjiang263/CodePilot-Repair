# Phase 5：单次修复闭环开发记录

日期：2026-10-13  
分支：`codex/phase5-repair-core`  
基线：包含 Phase 4 HEAD `3321de3`

## 实现范围

P-01～P-23 已实现。数据流为：Issue → 真实仓库索引/BM25 检索 → 两阶段 Localization → 确定性 ImpactAnalyzer → 只读 Planner → 有界 Coding Agent → 规范 Git diff → PatchValidator/AST 作弊检测 → FakeSandbox 或 DockerSandbox → JSON/Markdown Run Report。

- P-01～P-08：`ImpactAnalyzer`、风险预估、`RepairPlan`、只读 Planner、统一路径策略、读/检索/调用图工具。
- P-09～P-18：唯一命中的 Search/Replace、尾随空白降级与相似片段、create/diff/test/finish、11 工具注册表、Unified Diff、规范 Git diff、PatchValidator、AST 前后对比作弊检测与硬编码 warning。
- P-19～P-23：30 次硬上限 Coding Loop、真实 LangGraph `StateGraph`、失败也可落盘的 Run Report、`codepilot solve --repo ... --issue ...`、FakeLLM + FakeSandbox/真实 Docker E2E。

没有实现 Phase 6～9 的复现测试、失败诊断、多轮自动修复、完整回归或 Reviewer。`finish` 只结束生成循环，不代表修复成功。只有补丁校验通过且授权测试通过时状态才是 `provisionally_verified`。

## 安全与真实性边界

- Planner 仅可见 6 个只读工具；Coder 可见完整 11 工具，但没有 shell/exec。
- 路径在访问前 realpath/resolve，并拒绝绝对路径、UNC、`..`、symlink 逃逸、`.git` 和配置 denylist。
- `run_tests` 只接受既有测试相对路径或合法 pytest NodeID，由 Sandbox 固定模板执行；不接受命令、管道、重定向或解释器参数。
- 既有测试和关键配置默认不可修改；`allow_test_edits` 只能由可信构造参数提供。
- AST 规则是启发式防线，不宣称覆盖所有作弊。硬编码期望值只产生 warning，不直接拒绝。
- 报告中的 `provisionally_verified` 仅证明一个补丁通过 Validator 与所选测试；不证明全仓库无回归或 Issue 已最终解决。

## 定向验证

- P-01～P-08：14 passed（pytest）。
- P-09～P-18：受限执行环境阻止 pytest 创建临时目录；相同测试函数曾以直接 Python runner 执行通过。该结果不冒充 pytest，最终全量门禁结果另列。
- P-19～P-23：Fake E2E 9 passed / 1 docker deselected（pytest）；覆盖成功、patch rejected、tests failed、tool failure、environment error、budget exhausted、确定性与真实 `solve` CLI 装配。
- 真实 Docker E2E：Docker Desktop Engine 29.8.2 / Linux daemon；第一次真实运行暴露 fixture 环境缺少 pytest，状态正确为 `sandbox_error`，补齐受控 EnvSpec 后复跑为 **1 passed / 8 deselected**。证据明确区分 FakeLLM 生成与真实 Docker 执行。

## 最终门禁

最终代码与文档完成后执行：

- `ruff check .`：待最终记录。
- `ruff format --check .`：待最终记录。
- `mypy src`：待最终记录。
- `pytest -m "not docker and not llm" --cov --cov-report=term-missing`：待最终记录。

## Deferred Acceptance

- P-24 真实 LLM 修复验收：**NOT RUN**。未调用 DeepSeek、GLM、Kimi 或其他真实付费模型。
- Phase 5 Fake/Mock 结果不代表真实模型修复成功率。
- DA-001～DA-006 保持既有状态；DA-007 已关闭且没有证据支持重新打开。
- 未发现需要新增登记的 P0/P1 技术风险。Windows 受限执行器的 pytest 临时目录 ACL 问题是本次开发宿主限制，不是产品运行路径；最终门禁使用获批的标准 pytest 进程获取真实证据。
