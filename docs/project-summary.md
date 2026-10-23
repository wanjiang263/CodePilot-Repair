# CodePilot-Repair 项目说明

日期：2026-10-24（初稿 2026-10-21）。本文从工程架构、技术难点、测试质量和真实评测四方面总结当前交付。
所有数字均来自仓库内可复核的 artifact、测试输出或只读历史实验；**不使用模拟运行推导
SWE-bench 解决率**。

---

## 1. 工程架构

CodePilot-Repair 是一个“**API/Worker 分离 + 真实 LangGraph + 可冻结配置**”的仓库级修复
Agent：

```text
React Dashboard
  │ X-API-Key（同源，SSE 使用 fetch 流）
  ▼
FastAPI
  ├─ 鉴权 / 设置 / 任务创建 / SSE / Patch / Report / 只读历史
  └─ 只写 SQLite 队列，不执行 Graph / clone / Docker
       │
       ▼
SQLite（api_tasks / api_runs / api_events / settings）
       ▲ claim + heartbeat + lease
       │
独立 Worker（codepilot worker）
       │
       ▼
InferenceAssembly → M2 LangGraph
  ├─ 定位 / 影响分析 / 计划 / 编码 / 校验 / 目标测试
  ├─ 回归 / 诊断 / 独立审查 / finalize
  ├─ Budget + Reviewer/Test 预留 + Tool 限额
  ├─ 节点级 Checkpoint + 恢复
  └─ Provider Registry（OpenAI 兼容 / Anthropic Messages / Bedrock）
       │
       ▼
DockerSandbox / 显式 fake sandbox
Artifacts（artifacts/runs/<run_id>）
```

Phase 14 新增：

- **设置服务层** `src/codepilot/settings/`：类型化文档、SQLite 版本控制、审计、内置目录、
  加密凭据、SSRF 客户端、连接测试、角色路由与任务快照。
- **Settings API**：`/api/v1/settings/*` 覆盖读取、分区更新、Provider/Model/凭据、测试、
  导出/导入、运行时诊断、脱敏日志、文档与安全清理。
- **任务冻结**：`api_tasks.config_json/config_hash` 在创建时冻结，Worker 按快照装配；
  设置修改不会影响运行中任务。
- **前端设置中心**：General / Models / Agents / Permissions / Budget / Workspace /
  Advanced / About，双语 + 明暗主题，含 Provider/Model 对话框与凭据管理。
- **部署**：多阶段 Dockerfile、`docker-compose.yml`、健康检查、非 root 用户、持久卷、
  `.env.example`。

Phase 15 的局部增强：

- **状态可信度**：Provider/Model 视图拆分为配置完整性、凭据、连接测试、Live 路由条件与
  真实推理验证五个维度；连接测试结果持久化并绑定配置指纹，模型/凭据引用变化后标为过期。
- **Timeline 去重**：角色级事件（如 `reviewer`）并入对应图阶段（`review`），真实历史事件
  全部保留且可回放；重复执行的审查仍显示为多个阶段。
- **设置页信息密度**：低频操作收进菜单，模型表保留 ID、限制、工具声明、价格、阻塞项与
  状态；`xl` 以下设置导航改为横向，1100×800 无页面横向溢出。
- **前端拆包**：History/Benchmarks/BenchmarkDetail/Settings 按路由懒加载，主 bundle
  513 KB → 427.57 KB（gzip 148 KB → 131.98 KB），Vite 不再出现大 chunk 警告。
- **工程基线**：GLM14 费用双口径并列、skipped 测试逐项归因；多 Provider 路由/冻结/
  凭据撤销测试补齐；无付费预检脚本与 M3 分层计划交付。

## 2. 技术难点与处理

1. **“界面配置”必须真正生效**
   - 没有给 Web 写第二套调用器。设置被转换为现有 `LLMConfig` / `ProviderSpec` / `ModelSpec`，
     再走 `UsageTrackingProvider`、预算、重试、并发限制和 Graph 节点。
   - 任务快照包含协议、base URL、模型能力、价格、凭据引用与角色路由；Worker 只消费快照。
   - 任务级模型选择 > 角色覆盖 > 全局默认，CLI/YAML 路径保持不变。

2. **凭据不泄露**
   - 模型 Key 支持环境变量或 Fernet 密文；API 只返回 `kind/available/updated_at`。
   - 前端 Key 管理对话框保存后立即清空；任务快照与导出不包含明文。
   - 测试扫描 API 响应、artifact、事件表与导出，验证密钥不出现。

3. **自定义端点 SSRF**
   - 强制 HTTPS、拒绝私有/保留地址、禁用重定向与环境代理。
   - 自定义 `httpx` transport 在请求时解析并固定公网 IP，保留 `Host` 与 `sni_hostname`，
     防止 DNS 重绑定；响应大小与超时受限。

4. **真实 Provider 的协议差异**
   - OpenAI 兼容：沿用既有已验证实现。
   - Anthropic：原生 Messages 请求/响应映射、`tool_use`/`tool_result`、缓存 token 成本。
   - Bedrock：Converse API + AWS SDK 凭据链；不把 API Key 冒充 IAM；boto3 为可选依赖，
     未安装时目录标为不可用，并用确定性 fake client 测试映射与错误。

5. **预算 fail-closed**
   - Live 任务必须有正的费用硬上限；路由模型必须有完整价格和可换算币种。
   - 冻结预算与配置 hash；恢复/重试不会重置预算。
   - 证据：Phase 12 的 DA-024 通过“闭环准入 + 份额”解决，而不是调高 `max_test_runs`。

6. **国际化与主题**
   - `react-i18next` + 资源文件；组件不散落双语硬编码；测试校验键集合、占位符与
     组件引用。
   - Light/Dark/System 用 CSS tokens 与内联首屏脚本，避免白屏闪烁；Diff、终端、报告区域
     有独立明暗配色。

7. **单机部署**
   - API/Worker 共享 SQLite 与 `/data`；Worker 心跳文件供 Compose 健康检查。
   - 非 root 用户；只绑定回环地址；Docker socket 风险在安全文档中明确记录。

## 3. 测试质量

当前测试策略：

- **单元/集成**：设置版本冲突、凭据密文落盘、导入导出、路由优先级、Live 入队拒绝、
  Anthropic/Bedrock 适配、SSRF 负控制、连接测试三档区分。
- **工作流 E2E**：FakeLLM Web → Worker → M2 Graph → Report；SSE 断线续传；取消与恢复；
  MockTransport 下多 Provider 路由进入真实 Graph；设置修改不污染已运行任务；历史回放只读。
- **前端**：组件测试 + i18n 完整性 + 主题 System 响应 + 设置中心交互；Typecheck、ESLint、
  生产构建。
- **部署**：Compose 构建、启动、健康检查、API 鉴权、任务执行、设置持久化与清理。

本轮最终门禁（WSL2 Ubuntu 24.04 / Python 3.12.3，代码与 Windows 工作树同一份）：

- 后端 `pytest -m "not docker and not llm"`：**3584 passed, 8 skipped, 50 deselected**，
  exit 0，覆盖率 **88.59%**（阈值 80%）。
- `ruff check .` / `ruff format --check .` / `mypy src` 全部通过。
- 前端 Vitest **37 passed**，ESLint、TypeScript、Vite build 通过。
- Compose 实际启动 API + Worker 均为 healthy，API 鉴权、前端、离线任务、报告/patch、
  重启持久化与安全清理均验证通过。

Windows 原生 Git Bash 会话存在 MSYS `sh` 命名对象与用户符号链接权限的环境限制；该限制
导致部分工作区/symlink 单元测试在 Windows 全量运行中失败，但同一代码在 Linux 全量门禁
通过。Phase 14 的本地 Git transport 回退仅处理该 MSYS 启动错误，没有降低覆盖率阈值，
也没有跳过测试制造 PASS。详细记录见
[`docs/notes/phase14-product-settings-delivery.md`](notes/phase14-product-settings-delivery.md)。

Phase 13（6 skipped）到 Phase 14（8 skipped）新增的两项均为显式环境依赖：
`test_real_faiss_if_installed`（可选 `faiss` 未安装）与
`test_real_glm14_artifact_replay_keeps_verdicts_separate`（Phase 14 的 WSL 同步树未包含
历史 artifact）。当前 Windows 树中后者已实际执行并通过；没有新增无条件 skip。Phase 15
的定向验证与新增用例记录在
[`docs/notes/phase15-engineering-hardening.md`](notes/phase15-engineering-hardening.md)。

## 4. 真实评测

已归档的真实运行（不是模拟）：

| 运行 | 实例 | 模型 | 官方 verdict | 内部终态 | stop reason | 内部 Usage（调用 / CNY） | 派发账本（调用 / CNY） |
|---|---|---|---|---|---|---|---|
| GLM14 | marshmallow-code__marshmallow-1343 | glm-5.3-flash-main | RESOLVED | budget_exhausted | budget_test_runs | 26 / 0.19581048 | 25 / 0.19192368 |
| GLM13 | 同上 | 同上 | UNRESOLVED | stopped | repeated_patch | 19 / 0.17084768 | 19 / 0.17084768 |
| GLM12 | 同上 | 同上 | RESOLVED | error | workflow_error | 13 / 0.12327760 | 13 / 0.12327760 |
| GLM11 | 同上 | 同上 | RESOLVED | budget_exhausted | budget_tool_calls | 45 / 0.28167776 | 45 / 0.28167776 |

**GLM14 两组口径并存**：内部 Usage 的 26 次 / 0.19581048 是历史统计结果；派发账本的
25 条 settled / 0.19192368 是实际 HTTP 请求。差额 0.0038868 对应一次被重复记账的审查：
`usage/*.json` 的 `by_role` 同时有 `reviewer` 与 `review`，各 1 次且金额相同；
`events.jsonl` 的 `seq=44`（node=`reviewer`）与 `seq=45`（node=`review`）时间戳、耗时、
token 相同而 call_id 不同。修复提交 `a1afbaf` 将 `tracks_usage` 改为按账本对象沿装饰器链
查重，并有 `tests/unit/llm/test_tracking_chain_accounting.py` 覆盖；GLM11–GLM13 两组数字
原本一致。历史 artifact 未被重算或改写。

解读边界：

- 这 4 次运行是**同一实例的重复真实实验**，不是 4 个不同任务，不能据此计算解决率。
- 官方 verdict 由官方 Harness 决定；内部级别由 M2 closure 决定，两者独立。GLM14 的
  “官方 RESOLVED / 内部预算耗尽”正是必须保留的差异证据。
- Phase 12 的 779 条基线回归（25 项判据、0 次模型调用、0 条新增残留）是内部验证证据，
  不是官方 SWE-bench 批跑结果。
- Phase 14 的截图和演示任务使用 FakeLLM/fake sandbox，不进入上表。

## 5. 未完成与后续路线

- M3 正式 SWE-bench 批量评测：**未完成**。
- M4 消融实验、test50/test300、10 题费用校准：**未完成**。
- 真实 GitHub Actions CI：**未完成**。
- AWS Bedrock 真实调用：代码与 Mock 测试完成，真实 AWS 环境未验证（boto3 未安装）。
- Anthropic 真实调用：协议适配与 Mock 测试完成，未产生付费请求。
- 真实 Provider 冒烟与 Live M2 E2E：**NOT RUN**。Phase 15 完成了无费用预检：DeepSeek 与
  BigModel 凭据可解析、模型规格/价格/预算币种准入通过；但当前 Docker 命名管道访问被拒，
  且本阶段没有费用授权，因此未发起任何付费请求。
- 当前设置库没有真实推理验证记录；界面统一显示「真实推理未测试」，这不是失败率或成功率。
- 演示视频：**NOT RUN**（脚本已交付）。
- DA-022 的真实同形状复现：仍为 NOT OBSERVED，不因本轮 UI/部署完成而改变。

下一阶段建议：

1. 用 MockTransport 的全图 live 用例扩展到更多 Provider 协议，并在真实账号上做小额度校准。
2. 在独立 runner 主机上实现 Docker socket 代理与每任务资源配额，替代本地 socket 直挂。
3. 增加多用户身份、每用户设置与预算、审计导出。
4. 在真实 SWE-bench 子集上执行可复现的官方评测，并保留预测、成本与 artifact 的完整链路。

## Phase 18（2026-10-22）

- **已验证**：内部 M2 环境恢复（生产组件级）。生产链路在 `pvlib__pvlib-python-1072` 上完成补丁应用、1 项复现测试收集、未修复复现 FAIL、打补丁复现/目标/回归 PASS、JUnit 解析与 Graph 状态/路由回传；使用 `OfficialInstanceSandbox`、生产 JUnit/status 解析和 `execution_route`，未运行 `InferenceAssembly`/完整 LangGraph。实际镜像为 Phase 17 修复镜像 `sha256:e80d07e5…`，清理后无新增容器/卷，模型费用 CNY 0.00。定向测试 90 passed。
- **生产变更**：`SWEBenchInstanceImageResolver` + `OfficialInstanceSandbox` + `configs/swebench/instance-image-overrides.json`；镜像缺失或 ID 不匹配 fail closed，不回退官方标签；override 仅对 pvlib 生效。
- **部分完成**：Cal10 真实执行 7/10。有效官方评测 5（astropy、django、seaborn、flask、requests），官方 RESOLVED 2（astropy、seaborn），有效官方解决率 40.0%；内部链路形成终态 3/10（seaborn、flask、requests），但 seaborn 为 `failed/tests_failed`、requests 为 `stopped/diagnosis_give_up`，不等于验证通过；内部声称成功仅 1/10（flask）。官方与内部口径未混用。
- **停止**：`pydata__xarray-4493` 第 3 次调用 `ReadTimeout`，派发账本 `unknown_frozen`；保留 CNY 0.811469 未知预留并停止后续派发，`pylint`、`pytest`、`scikit-learn` 未执行。
- **费用**：已结算 CNY 1.056919 + 未知预留 CNY 0.811469 = 记账上界 CNY 1.868388；单题最大 0.831435 ≤ 3.00，整批 ≤ 30.00，剩余 28.131612。费用对账按保守上界完成（`reconciliation_complete=true`、`unreconciled_cost_calls=0`、`actual_usage_unknown_calls=1`）；xarray 超时调用实际用量不可见，不能声称与服务商账单一致，也不按零计费。
- **未执行**：L3 test50、L4 M4/test300。详细证据见 `docs/notes/phase18-m2-environment-and-l2-cal10.md` 与 `artifacts/phase18/`。

## Phase 18E（2026-10-23/24）

> 本节是 l2cal10e 追加尝试的最终口径；上方 Phase 18 段落保留为原 Cal10 历史快照（截至 xarray 停止点）。

- **pytest P2：`NOT VERIFIED`。** 最后 attempt 为 `l2cal10e-a21`，内部验收 `REJECTED`（`accepted=false`）。真实 Docker 图运行已发生，但 `target_test_passed=false`、`regression_clean=false`、`review_executed=false`；模型没有得到满足六项验收的补丁，`stop_reason=repeated_patch`。不是预算、环境或 Harness 故障。
- **官方评测边界：** pytest 没有运行官方 Harness，官方 verdict 只能是 `NOT_EVALUATED` / `DEFERRED`，不得写成 `UNRESOLVED`、`RESOLVED` 或「已评测」。
- **sklearn：** 既有内部验收为 `ACCEPTED`（`acceptance-l2cal10e-a7-recheck.json`；原始 acceptance 为 `REJECTED`，保留追溯），官方 verdict 为 `UNRESOLVED`。本轮没有重跑 sklearn。
- **未重跑：** `pydata__xarray-4493`、`pylint-dev__pylint-6506` 与原 `l2cal10c` 的既有结果均未重跑。
- **金额（CNY）：** pytest 单题累计上界 `7.80709616`；sklearn `0.62403504`；批次已结算 `7.18344160`；未知预留 `4.05734400`（5 次，绝不按零计费）；批次累计上界 `11.24078560`；pytest 剩余单题预算 `12.19290384`；批次剩余预算 `88.75921440`。
- **护栏：** 20/100 未触发；停止原因是补丁未满足验收，不是护栏。
- **受保护哈希：** 12/12 一致（`artifacts/phase18/l2-cal10e/preflight/protected-verification.json`，`all_match=true`）。
- **registry：** 新版 `attempts.json` 共 25 条（pytest 21、sklearn 4），25/25 账本存在且 SHA-256 与 registry 一致；数组按 `(instance_id, attempt_label)` 字典序排列，末尾不是 a21，最后 attempt 以 batch status 与 a21 manifest 为准。
- **旧汇总边界：** 顶层 `cost-ledger-reconciliation-l2cal10e.json`、`official-evaluation-summary-l2cal10e.json`、`phase18e-summary.json` 只覆盖 a9 时点，不能充当 a10–a21 的最终汇总。最终口径指向 a21 manifest / ledger / acceptance / report / batch status 与新版 `attempts.json`。
- **停止派发：** 不得继续派发 a22 或任何新实时运行。
