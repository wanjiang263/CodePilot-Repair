# Phase 13：API 服务化与可视化 Dashboard

日期 2026-10-21。分支 `codex/phase12-real-issue-pilot`（沿用当前工作树），开工 HEAD
`b4effac`。本轮不 commit / 不 push / 不创建 PR / 不 merge，也不改写历史实验 Artifact。
默认执行后端是 **FakeLLM + 确定性 fake sandbox**；没有调用任何付费 Provider，没有发起
SWE-bench 批跑或官方 Harness 复评。

---

## 1. 交付摘要

| 项 | 状态 |
|---|---|
| A-01 FastAPI + API Key | DONE |
| A-02 创建任务 | DONE |
| A-03 任务/分页查询 | DONE |
| A-04 SSE 事件流 + `after_seq` 续传 | DONE |
| A-05 Patch / Report + 身份校验 | DONE |
| A-06 幂等取消 + 安全边界停止 | DONE |
| A-07 Benchmark 指标（只读历史） | DONE |
| A-08 独立 Worker + SQLite 队列 + 恢复三态 | DONE |
| A-09 仓库 URL 白名单 / SSRF 负控制 | DONE |
| A-10 React 工作台 | DONE（本地浏览器实测） |
| A-11 `codepilot report --html` | DONE |
| A-12 API / 前端 / E2E 测试 | DONE |
| A-13 compose | **NOT RUN**（Phase 14 范围） |
| 真实付费 Provider / test50 / test300 / CI | **NOT RUN** |
| DA-022 真实同形状 | **REAL SAME-SHAPE NOT OBSERVED**（不变） |
| M4 消融 / 统计报告 | **未完成**（不因 Phase 13 标为完成） |

---

## 2. 后端架构

```text
Browser (React/Vite, same-origin or /app)
   │  X-API-Key
   ▼
FastAPI (api/app.py)
   │  durable INSERT (queued)
   ▼
SQLite queue/events (api/database.py)
   ▲                         │
   │ claim / heartbeat       │ independent process
   │                         ▼
   └──────────────  codepilot worker (api/worker.py)
                              │
                              ▼
                    InferenceAssembly → real M2 LangGraph
                    (nodes / routing / budget / checkpoint)
                              │
              EventSink → events.jsonl + api_events
              ArtifactStore → artifacts/runs/<run_id>/
```

- API 进程只做鉴权、校验、持久化和读取，不执行 Graph、Git clone 或 Docker。
- Worker 复用 `InferenceAssembly`、现有 Checkpointer、ResultJournal/Operation Journal、
  RunReport 与 EventSink。仓库级任务使用 `enforce_base_only=False`（评价夹具专用约束
  不适用于普通用户仓库）；演示任务使用服务端 workspace factory，避免宿主 MSYS shell
  不可用时的本地 clone 依赖。
- 取消只在节点边界协作式生效，不杀 Docker daemon、不触碰其他项目容器；已发生的
  副作用、补丁、日志和费用保留。
- 每个 run 的 artifact 目录是 `artifacts/runs/<run_id>/`；API 只按数据库中的
  task/run 身份解析，不允许前端传文件路径。

### 关键文件

| 文件 | 作用 |
|---|---|
| `src/codepilot/api/app.py` | 应用工厂、中间件、异常信封、health、静态 `/app` |
| `src/codepilot/api/auth.py` | `X-API-Key`、启动校验 |
| `src/codepilot/api/security.py` | URL 白名单、路径清洗、脱敏 |
| `src/codepilot/api/schemas.py` | 请求/响应契约 |
| `src/codepilot/api/database.py` | SQLite 队列、run 状态、事件归档、恢复 |
| `src/codepilot/api/routes.py` | tasks / runs / SSE / patch / report / cancel / benchmarks / history / meta |
| `src/codepilot/api/worker.py` | 独立 Worker、心跳、取消、恢复与失败归因 |
| `src/codepilot/api/demo.py` | 演示仓库 materialize、FakeLLM 脚本、确定性 fake sandbox |
| `src/codepilot/api/history.py` | YAML 白名单目录、只读历史回放、完整性核对 |
| `src/codepilot/api/reporting.py` | Artifact → API 视图归一化、patch 解析 |
| `src/codepilot/api/tasks.py` | 交给装配层的五字段任务视图 |
| `src/codepilot/html_report.py` | 自包含静态 HTML（Jinja autoescape） |

### 核心修改（不改变预算/评测语义）

- `RepairState` 新增 `RunStatus.CANCELLED` 与 `StopReason.CANCELLED`；`terminated()` /
  finalize 沿用既有“终态短路”，取消不会被写成成功或 M2。
- `graph/builder.py` 每个节点边界检查可选 `cancel_check`，并写入真实 `node_start` 事件；
  取消时只改终态、原因和审计，保留节点已产生的部分更新。
- `graph/deps.py` 只增加可选 `cancel_check`；默认 `None`，CLI/评测行为不变。
- `eval/assembly.py` 增加可选 `env_spec_factory` / `workspace_factory` /
  `enforce_base_only` / `event_store` / `cancel_check`；默认值保持原有评测路径不变。
- `observability/events.py` 引入 `EventStoreProtocol`，让 API Worker 把真实 EventSink
  事件镜像进队列数据库。
- `repo/workspace.py` 支持只作用于克隆的 `git_config`（`http.followRedirects=false`），
  并把本地路径以 POSIX 形式交给 git。

---

## 3. 前端架构

`web/`：React 18 + TypeScript + Vite 6 + Tailwind 3.4 + Lucide；自建设计 tokens 与
轻量组件，不引入 UI 框架、图表库或重型动画依赖。

页面结构：

- 左侧项目/最近任务/历史回放导航，可折叠；`Ctrl+K` 全局搜索。
- 中间任务会话：Issue、Agent reading、按节点分组的时间线、工具调用、预算读数、
  最终结论；Summary / Normal / Detailed 三档信息密度；自动滚动与暂停、SSE 重连提示。
- 右侧上下文面板：Changes / Files / Tests / Review / Run / Report，可拖拽宽度、折叠；
  小屏自动成为覆盖层。
- 新建任务：居中的单输入区 + 仓库选择 + 离线模式 + 紧凑预算选择器。
- 历史与 Benchmarks：同一会话界面回放；官方 verdict 与内部验证级别分开显示。
- 浅色/深色主题、Skeleton、Empty/Error/Cancelled/Completed 状态、取消确认、键盘焦点。

API key 不在源码/构建产物中：页面要求操作员输入，保存在 `sessionStorage`，只通过
`X-API-Key` 头发送；SSE 使用带头的 `fetch` 流而不是原生 EventSource。

---

## 4. API 清单

| Method | Path | 说明 |
|---|---|---|
| GET | `/health` | 需 key；只返回 `{status}` |
| GET | `/api/v1/meta` | 白名单域名、演示仓库、执行模式、预算默认值 |
| GET | `/api/v1/openapi.json` | 需 key 的 OpenAPI schema（`/docs` 默认关闭） |
| POST | `/api/v1/tasks` | 创建任务，返回 task/run 身份 |
| GET | `/api/v1/tasks` | 分页、状态/仓库/文本筛选 |
| GET | `/api/v1/tasks/{task_id}` | 任务详情、原始状态、停止原因、摘要 |
| GET | `/api/v1/runs/{run_id}/events` | SSE，支持 `after_seq` 与 `Last-Event-ID`，心跳、终态结束 |
| GET | `/api/v1/runs/{run_id}/patch` | 最终 diff + 文件级查询 `?file=` + 身份校验 |
| GET | `/api/v1/runs/{run_id}/report` | 归一化报告 + markdown + 工具/测试/审查/预算 |
| POST | `/api/v1/runs/{run_id}/cancel` | 幂等取消；queued 直接停止，running 协作式 |
| GET | `/api/v1/benchmarks/{bench_id}` | 只读历史指标与完整性核对 |
| GET | `/api/v1/benchmarks` | 历史 benchmark 列表 |
| GET | `/api/v1/history` | 历史回放列表（同一会话界面） |

状态保留底层原文：`succeeded` / `provisionally_verified` / `budget_exhausted` /
`stopped` / `failed` / `error` / `cancelled` / `uncertain`；UI 归一化只是分组，
预算停止显示为 **Stopped**，不会显示成模型修复失败。

---

## 5. 启动方式

```bash
export CODEPILOT_API_KEY='<至少 16 字符>'

# API（默认 127.0.0.1:8000，未配置 key 拒绝启动）
uv run codepilot api

# 独立 Worker（另一个终端）
uv run codepilot worker
# 只执行一个任务：
uv run codepilot worker --once

# 前端开发服务器（代理 /api 到 8000）
cd web && npm install && npm run dev        # http://127.0.0.1:5173
# 或构建后由 API 同源提供
npm run build                               # FastAPI 挂载 /app
```

CLI：

```bash
uv run codepilot report <run_id> --html
uv run codepilot report --run-dir artifacts/runs/<run_id> --html --output report.html
```

历史回放目录在 `configs/benchmarks.yaml`（服务端只读白名单），包括
`glm11`～`glm14` 单题产物；打开 `#/replay/<bench_id>` 或点击侧栏 “Replay archive”。

---

## 6. 验证证据

### 6.1 后端

`tests/api/` 覆盖：

- 未配置/空白/过短 key 拒绝启动；所有受保护端点（含 health、SSE、cancel、
  benchmarks、openapi）缺失/错误 key 返回 401。
- 任务创建/分页/详情；API 进程重启后持久队列仍在。
- Worker 真实 M2 Graph（FakeLLM + 确定性 fake sandbox）→ `succeeded` /
  `m2_verified` / patch / report；checkpoint 文件落盘；事件含 node_start/plan/review。
- SSE：`after_seq` 与 `Last-Event-ID` 续传、单调、不重不漏；运行中一直流到终态。
- 取消：queued 直接取消；claimed/running 取消；图执行中在节点边界取消并 finalize
  为 `cancelled`；重复取消幂等；已完成运行不被改写为 cancelled。
- 恢复三态：有终态 → `recovered_terminal_state`；有 checkpoint+workspace →
  `pending_resume`；无证据 → `uncertain` / `worker_lost`。
- URL 正反例：`file://`、本地路径、相对路径、IP/localhost、非白名单域、用户凭据、
  端口、query/fragment、编码/路径穿越全部拒绝；白名单 HTTPS 接受。
- Patch/Report 身份：篡改 patch 或跨 run report 返回 409；`?file=` 穿越 400；
  report raw 不含绝对路径/钥匙。
- 历史只读：官方 `RESOLVED` 与内部 `budget_exhausted` 分开；完整性 5 项全通过；
  回放前后目录哈希不变；cancel 返回 409。
- 静态 HTML：关键字段、官方/内部分离、未执行阶段、XSS 转义；CLI HTML/Markdown。
- 密钥/路径：artifact 全树与 `api_events` 中搜索 API key 为空。

### 6.2 前端

`web` Vitest 25 项：diff 行号/分屏/token、状态映射、SSE block/重连、任务表单校验、
状态徽章、官方/内部分离、历史列表、预算读数、失败历史。

### 6.3 浏览器实测

在本地实际启动 API、Worker、Vite 后，用外部 Edge（User Browser Connector）完成：

1. 首次连接页输入 key → 进入工作台；
2. 从 “Use example issue” 创建演示任务 → 会话页实时显示 node 启动、计划、编码、
   目标测试、回归、审查、finalize；右侧 Changes 显示 1 文件 diff、identity verified；
3. 完成后 Final outcome 显示 `M2 verified`、目标测试 PASS、回归 complete、审查 approved；
4. 打开 Benchmarks → GLM14 详情，官方 **RESOLVED** 与内部 **Budget exhausted** 并排，
   完整性 5 项全通过；打开事件回放复用同一会话界面；
5. 切换到浅色/深色主题、Ctrl+K 命令面板、折叠侧栏、重置面板宽度；
6. paused Worker 下创建任务 → 取消确认框 → 终态 `cancelled`，无 patch/report，
   目标测试/回归/审查均为 NOT EXECUTED（修掉了跨会话状态残留）。

截图不在仓库中留存（运行产物目录被 `.gitignore` 排除）；复现命令见 §5。

### 6.4 真实历史 Artifact

- GLM14：官方 `RESOLVED`，内部 `budget_exhausted`，`stop_reason=budget_test_runs`，
  补丁 `a21b0efd…`，67 条事件，完整性 required/run-id/sequence/patch/official 全 PASS。
- GLM13：官方 `UNRESOLVED`，内部 `stopped` / `repeated_patch`。
- GLM12：官方 `RESOLVED`，内部 `error` / `workflow_error`（不合并）。
- GLM11：官方 `RESOLVED`，内部 `budget_exhausted` / `budget_tool_calls`。
- 回放严格只读；目录哈希在 API 调用前后保持一致。

### 6.5 统一门禁

（见文末“门禁结果”一节，最终数字在全部功能完成后一次执行。）

---

## 7. 安全边界与残余风险

已实现：

- 未配置/过短 `CODEPILOT_API_KEY` 拒绝启动；所有 API 需 key；默认 127.0.0.1；
  无 CORS 中间件；文档页关闭，OpenAPI 需 key。
- 请求体 256 KiB 上限；字段长度/枚举/预算范围校验；统一错误信封不回显原始输入。
- 仓库只接受服务端白名单 HTTPS 或服务端 demo 名；拒绝凭据、query、路径穿越、
  百分号编码、IP、localhost、非白名单域；Worker 远端 HEAD 与 clone 禁用重定向。
- run/patch/report 只按数据库身份或只读 catalog 解析；`?file=` 路径限 patch 内。
- 事件/报告/错误响应统一脱敏并清洗已知宿主根；API key 不写日志/事件/Artifact/前端包。
- 取消不触碰 Docker daemon 或其他项目容器；不重放不确定的外部副作用。

残余风险：

- 单用户、单 Worker、SQLite 队列；不是多租户/分布式调度。
- 白名单域名 + 禁用重定向降低了 SSRF，但未做 DNS pinning；服务端配置本身是信任边界。
- 默认只有 FakeLLM/确定性 fake sandbox；真实付费 Provider 未接入 Web。
- 本机 Windows 的 Qoder agent 沙箱不允许从 Python 子进程启动 MSYS `sh`，因此本机的
  HTTPS 仓库 `git clone` 路径无法实测；演示任务走服务端 workspace factory 已实测。
  真实 HTTPS 仓库请在 WSL/Linux 或正常 Git 环境运行。
- 历史 catalog 允许管理员配置仓库外绝对路径；这是服务端配置信任，不是客户端输入。
- `A-13 docker compose` 未实施；CI/test50/test300/10 题费用校准仍 NOT RUN。
- 真实模型效果、DA-022 同形状、M4 消融均未因本轮变化。

---

## 8. 未完成事项

- A-13 `docker compose up`、A-14 README 全量、A-15 架构/API 文档、A-16 安全文档、
  A-17 demo 录屏、A-18 简历摘要、A-19 收尾一致性：**NOT RUN / 不在本轮范围**。
- 真实付费 Provider 的 Web 接入（需要独立授权与预算）——未实现；本轮所有 Web 运行均
  明确标记 simulated。
- GitHub Actions（分支未 push）**NOT RUN**；test50 / test300 / 10 题费用校准 **NOT RUN**。
- DA-022 仍为 **REAL SAME-SHAPE NOT OBSERVED**；DA-023/DA-024 维持既有工程修复状态；
  M3/M4 不因本轮 Dashboard 交付而被宣称完成。

---

## 9. 门禁结果

所有后端命令在 WSL2 Ubuntu-24.04（Python 3.12.3，最终同步树）执行；前端在 Windows
Node 25.2.1 / npm 11 上执行。没有为了绿灯降低覆盖率阈值、新增无依据 skip 或弱化断言。

| 门禁 | 命令 | 结果 |
|---|---|---|
| 静态 | `ruff check .` | **All checks passed**，exit 0 |
| 格式 | `ruff format --check .` | **413 files already formatted**，exit 0 |
| 类型 | `mypy src` | **Success: no issues found in 182 source files**，exit 0 |
| 全量离线 | `pytest -m "not docker and not llm" --cov --cov-report=term` | **3555 passed / 6 skipped / 50 deselected**，exit 0，293.47s；覆盖率 **90.29%**（80% 阈值未下调） |
| API 定向 | `pytest tests/api -q --no-cov` | **106 passed**（Windows 与 WSL 均通过） |
| 前端 | `npm test` / `npm run lint` / `npm run typecheck` / `npm run build` | **25 passed**；ESLint 0 warning；tsc 无错误；Vite build 成功（`dist` 296 KB JS + 38 KB CSS，字体本地打包） |
| 真实历史回放 | `tests/api/test_phase13_history.py::test_real_glm14_artifact_replay_keeps_verdicts_separate` | 在 WSL 实际读取同步后的 glm14 artifact，**通过**；5 项完整性全 PASS |
| 静态 HTML | `codepilot report ... --html` | 真实 glm14 与 Web demo 均生成；demo HTML 的 `Closure admission remaining=10` 与 API 读数一致 |
| 浏览器 | 外部 Edge（User Browser Connector）打开 Vite 与 `/app/` | 创建、实时执行、取消、Benchmarks、历史回放、浅/深色、Ctrl+K 均实际渲染成功 |

未重跑（本轮没有修改对应代码）：

- `pytest -m docker`：未修改 `DockerSandbox` / `RegressionRunner` / 执行隔离；
  继续引用 Phase 12 的 **50 passed / 0 failed** 证据。
- 779 条基线 NodeID 验证：继续引用 Phase 12 的 **25 项判据全 PASS** 证据。
- test50/test300、10 题费用校准、GitHub Actions：**NOT RUN**。
- DA-022 真实同形状：**REAL SAME-SHAPE NOT OBSERVED**。

唯一增加的 skip 检查：移除 `_PLANNED` 后，原参数化“未实现命令返回 2”的测试改为断言
`_PLANNED == {}`，因此最终 skip 仍为既有的 **6 个**（Windows 专属与 embedding 模型未设置），
没有新增 skip。

复现实测命令见 §5；实验 artifact 在 `artifacts/phase13-ui/`（被 `.gitignore` 排除，不提交）。
