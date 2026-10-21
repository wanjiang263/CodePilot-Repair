# Phase 15：工程基线收口、界面精修与真实模型验收准备

日期：2026-10-22。分支 `codex/phase12-real-issue-pilot`，开工 HEAD `b4effac`。
本阶段保留全部 Phase 13–14 工作树内容与历史 artifact；未执行 `reset`、`stash`、`rebase`、
merge 或 push；未修改 GLM11–GLM14 原始运行产物、`configs/benchmarks.yaml` 或只读回放数据；
**未发起任何付费模型请求，实际费用 CNY 0.00**。

Phase 15 提交：`a9d3620`（本地提交，未 push；含 Phase 13–15 冻结内容）。基线 `b4effac`；
本文档随后仅做提交哈希补记，最终 HEAD 见 `git log --oneline -2`。截图与浏览器验收对应
`a9d3620` 的界面代码，文档补记不改变界面。

---

## 1. 环境与基线

| 项 | 值 |
|---|---|
| 操作系统 | Windows 11 (10.0.22631)，Git Bash |
| Python | 3.12.7（`uv run`） |
| Node / npm | 25.2.1 / 11.6.2 |
| 后端测试 | 定向 pytest；未跑全量 `pytest -m "not docker and not llm"` |
| 前端测试 | Vitest 定向文件；lint / typecheck / build |
| Docker | Git Bash CLI 可用；Python/Docker SDK 访问命名管道被拒（见 §6） |
| 真实 Provider | 无付费调用；预检仅本地检查 |

Phase 13–14 的未提交内容全部保留，并在本阶段本地提交中一并冻结；没有改写历史 Git。
`.env`、`artifacts/`、`.playwright-mcp/`、临时目录与运行数据不进入提交。

---

## 2. GLM14 费用口径核查（P15-01）

### 2.1 结论

| 口径 | 来源 | 调用数 | 费用（CNY） |
|---|---|---|---|
| 内部 Usage | `artifacts/phase11-glm14-20261020/run-artifacts/usage/*.json` | 26 | 0.19581048 |
| 实际派发账本 | `run-artifacts/dispatch-ledger.json` | 25（全部 settled） | 0.19192368 |

两组数字都是真实历史读数，**并列保留**；差额 `0.0038868` 等于一次审查费用。

### 2.2 可复核证据

1. Usage 的 `by_role` 同时包含 `reviewer`（1 次）与 `review`（1 次），两者的
   `input_tokens=2993`、`output_tokens=533`、`cost=0.0038868` 完全相同。
2. `events.jsonl` 的 `seq=44`（node=`reviewer`, role=`reviewer`）与 `seq=45`
   （node=`review`, role=`review`）时间戳相同（13:35:24.806/807）、duration 同为 13171 ms、
   token 相同，但 call_id 不同。实际 HTTP 请求只有 25 条。
3. GLM11/12/13 的 Usage 与派发账本逐项一致（45/13/19 次，费用相同），说明差异只发生在
   GLM14 的 Reviewer 路径。
4. 根因是 Reviewer 在冒烟装饰器链上被套了两层 `UsageTrackingProvider`；修复提交 `a1afbaf`
   的 `tracks_usage` 改为沿 `inner` 按同一 `Usage` 账本对象查重。回归测试
   `tests/unit/llm/test_tracking_chain_accounting.py` 覆盖“26 次 vs 25 行”的同一形状。
5. 历史 artifact 未被重算、覆盖或“修正”；README、`docs/project-summary.md` 与本节均并列
   标注两种口径。
6. 定向测试还发现 Windows 索引器可能瞬时持有派发账本目标文件，导致 `os.replace` 返回
   PermissionError。`DispatchBudget._save` 增加有界重试（最多 6 次、递增 50–300ms），并新增
   `test_ledger_replace_retries_a_transient_windows_lock`；预占仍先落盘再发请求。

### 2.3 skipped 测试

Phase 13 Linux 门禁为 **6 skipped**，Phase 14 为 **8 skipped**。新增两项：

| 测试 ID | 跳过原因 | 当前树结果 |
|---|---|---|
| `tests/unit/retrieval/test_semantic_backends.py::test_real_faiss_if_installed` | 可选 `faiss` 未安装；其余 FAISS 回退路径用 monkeypatch 正常执行 | 当前 Windows venv 已安装 `faiss`，**实际执行并通过** |
| `tests/api/test_phase13_history.py::test_real_glm14_artifact_replay_keeps_verdicts_separate` | Phase 14 的 WSL 同步树没有 `artifacts/phase11-glm14-20261020/run-artifacts` | 当前树 artifact 存在，**实际执行并通过** |

两项均由 `pytest.importorskip` / `skipif` 显式触发，不是被屏蔽的失败；没有新增无理由 skip、
没有降低覆盖率阈值。其余 6 项为 Windows 专属 junction/taskkill 用例与未设置 embedding 模型
的既有环境 skip。

复核命令：

```text
uv run pytest tests/unit/retrieval/test_semantic_backends.py::test_real_faiss_if_installed \
  tests/api/test_phase13_history.py::test_real_glm14_artifact_replay_keeps_verdicts_separate \
  -q --no-cov -rs
# 2 passed in 3.97s
```

---

## 3. Timeline 重复审查（P15-02）

### 3.1 根因

前端 `groupEvents` 只按原始 `event.node` 分组。后端同一次审查既有角色层事件
`node=reviewer`，又有图节点事件 `node=review`；两者被渲染成两个同级「独立审查」阶段。
GLM14 历史 artifact 里还叠加了 §2 的重复记账，因此旧运行会看到两条几乎相同的审查信息。

### 3.2 修改

- `web/src/lib/stages.ts`：增加阶段族（`review/reviewer`、`localize/localizer`、
  `reproduce/reproducer`、`plan/planner`、`diagnose/diagnoser`）；角色事件在相邻图阶段内
  合并；`coder` 事件并入 `generate_patch` / `repair_patch` 当前阶段。
- 不删除、不改写任何事件：`NodeGroup.events` 仍保留原始 event 对象与顺序；详情里仍能看到
  两条历史重复事件及其原始 node。
- 真正重复执行的审查（不同轮次、中间隔了修复阶段）仍形成两个独立组。
- `Timeline.tsx` 增加 `data-testid="timeline-stages"`，供测试/浏览器验收精确检查阶段列表。

### 3.3 测试

`web/src/lib/stages.test.ts` 新增：

- reviewer 角色事件并入 review 阶段，原始 node 保留；
- 两次真实审查之间隔了 repair，仍为两个阶段；
- 历史重复的 reviewer + review llm_call 都保留在同一组详情；
- localizer/planner 角色事件并入对应图阶段；
- 孤儿 reviewer 事件仍标为 review 阶段。

浏览器检查：离线演示运行的时间线中「独立审查」阶段数 = 1（旧实现为 2）。

---

## 4. Provider 页面与状态可信度（P15-02）

### 4.1 信息密度

- Provider 头部的编辑/凭据/发现/加模型/删除收进 `…` 菜单；保留「校验配置」按钮。
- 模型行保留：模型 ID、显示名、默认标识、来源（模板/手动/发现）、上下文/输出、工具声明、
  价格与币种、价格来源提示、路由条件、阻塞项、真实推理状态、测试时间、失败详情。
- 模型行动作（设为默认/编辑/测试推理/删除）收进行内菜单；收藏星保留。
- 长模型 ID 使用 `break-all` + `title`；长显示名截断但有完整 `title`；1100×800 下设置导航
  改为横向、内容全宽，表格操作列可达，页面无横向溢出。
- 搜索、收藏、凭据替换、模型发现、连接测试、模型编辑全部保留。

### 4.2 五态分离（服务端 + UI）

`SettingsService.model_view()` 新增 `verification`：

| 维度 | 字段 | 说明 |
|---|---|---|
| 配置完整性 | `config_complete` / `missing` | 模型规格与价格必填项 |
| 凭据 | `credential.available/reason` | 只描述引用是否可解析，不返回明文 |
| 协议 | `protocol.available/reason` | Anthropic/OpenAI/Bedrock 可用性 |
| Live 路由资格 | `routing.eligible/blockers` | 与 `_admit_model` 同口径：启用、凭据、协议、容量、价格、汇率 |
| 真实推理 | `inference.state/checked_at/detail` | `not_tested` / `verified` / `failed` / `stale` |

连接测试结果写入新的 `settings_connection_tests` 表（provider/model/kind/ok/fingerprint/
detail/checked_at），并绑定配置指纹：

- 配置校验、模型发现、真实推理分别记录；
- 配置校验成功**不会**把模型标为「真实推理已验证」；
- 模型字段或凭据引用变化后，旧推理记录变为 `stale`；
- 只有存在成功 inference 记录且指纹匹配才显示 `verified`；
- 无记录统一显示「真实推理未测试」，不编造时间或成功率。

安全边界同步收紧：`build_llm_config(snapshot)` 现在核对快照中的凭据引用与当前设置一致；
撤销/替换后旧任务返回 `credential_missing` / `credential_changed`，即使旧环境变量仍存在也不会
静默沿用。`docs/06-security.md` 已同步。

### 4.3 主题

只调整已有 tokens 与 disabled 透明度：

- Light：`--subtle` 150→116（对白底约 5.0:1）、`--line` 231→218、`--elevated` 从与 surface
  相同改为 247/245/240（hover/选中可见）；
- Dark：`--subtle` 122→138（对 surface 约 4.6:1）、`--line` 52→58；
- `.btn` / `.icon-btn` / Segmented / 菜单 disabled 透明度 40–50%→60%，文字更可读；
- 保留低饱和绿色强调色，没有新增装饰色。

真实 Chrome 验证 Light / Dark / System：System 首次加载跟随 OS，运行中切换
`prefers-color-scheme` 会即时更新；刷新后语言与主题持久化。

### 4.4 前端拆包

`App.tsx` 对 History / Benchmarks / BenchmarkDetail / Settings 使用 `React.lazy` +
`Suspense`；根 `ErrorBoundary` 提供失败后的整页重载重试。构建结果：

| 指标 | Phase 14 | Phase 15 |
|---|---|---|
| 主 JS | 约 513 KB / gzip 148 KB | **427.57 KB / gzip 131.98 KB** |
| 设置 chunk | 同主包 | 84.50 KB / gzip 19.36 KB |
| Vite 大 chunk 警告 | 有 | **无** |

历史/基准/详情 chunk 分别约 5.17/2.68/8.24 KB；新建任务仍随主包加载，设置与回放按需加载。

---

## 5. 多 Provider 测试矩阵（P15-03）

新增 `tests/unit/settings/test_phase15_routing_matrix.py`：

- 全局默认、Role Override、任务级覆盖；同一供应商多模型；不同供应商承接不同角色；
- 冻结快照在设置修改后不漂移；任务覆盖生成不同 config hash；
- 环境变量缺失、凭据引用撤销、价格缺失、协议不可用均 fail closed；
- Anthropic 与 OpenAI 协议从快照进入 Registry 后选择正确适配器；
- Bedrock 通过快照 + 确定性 fake client 调用，usage/cost/role 记账正确。

`tests/api/test_phase14_settings_api.py` 新增真实 M2 Graph 的 Anthropic 原生 Messages E2E：
MockTransport 收到的是 `/v1/messages`、`x-api-key`、`anthropic-version`，无
`Authorization`；完整离线演示任务完成并产出 patch；所有角色都走原生协议，Key 不出现在
artifact。该测试证明**协议与图接线**，不证明真实 Anthropic 服务可用。

既有测试继续覆盖：OpenAI 多 Provider 图路由、鉴权脱敏、限流、上下文超限、Anthropic
`tool_use`/`tool_result`/缓存 token、Bedrock 错误映射、SSRF/重定向/响应上限、暂停恢复与
Reviewer 不重置预算。未新增付费调用。

---

## 6. 无费用预检与冒烟准备（P15-04）

命令：

```text
uv run python scripts/phase15_provider_preflight.py \
  --output artifacts/phase15/provider-preflight.json
```

结果（2026-10-22）：

- 实际 Settings 库：**0 个已配置 Provider**。
- 候选模板中凭据可解析且模型准入通过：**DeepSeek**（`deepseek-flash`，CNY 2/8 每 Mtok）
  与 **BigModel**（`glm-4.7` / `glm-5` / `glm-5.3` / `glm-5.3-flash`，CNY 价格完整）。
- Z.AI：凭据可解析，但 USD 价格缺 CNY 汇率 → `currency_rate_missing`。
- OpenAI / Anthropic：无对应环境变量 → `credential_missing`，且模板无价格/上下文。
- Bedrock：未安装 `boto3` → 协议不可用；未发起 AWS 请求。
- 服务端：`live_llm_enabled=true`、`execution_backend=fake`、模拟 Live 沙箱逃生门关闭；
  单任务费用上限配置值 1000（实际冒烟应显式设更小硬上限）。
- Docker：Git Bash 直接执行 `docker version` 可返回 29.8.2，但预检的 Python 子进程与 Docker
  SDK 访问 `npipe:////./pipe/dockerDesktopLinuxEngine` 均返回 permission denied；Worker 运行在
  Python 进程中，因此 **Live M2 E2E 记 BLOCKED**，需修复 Docker Desktop/命名管道后重跑。
- 预检没有网络请求、没有模型发现、没有推理；不读取/打印凭据值。

### 真实推理测试计划（等待授权，未执行）

| 项 | 计划 |
|---|---|
| 首选 Provider / 模型 | BigModel `glm-5.3-flash`（接口 `glm-5.3-flash-main`，已有 CNY 价格） |
| 备选 | DeepSeek `deepseek-flash` |
| 协议 / 入口 | OpenAI Chat Completions；`POST /api/v1/settings/providers/{id}/test`，`kind=inference`, `confirm_cost=true` |
| 请求 | 单次最小推理，`max_output_tokens=16`，不启动 Coding Agent |
| 价格来源 | `configs/llm.yaml` 2026-10-18 公开价目快照：输入 0.8 / 输出 2.8 / 缓存 0.23 CNY 每 Mtok |
| 预期费用 | 远低于 0.01 CNY；任务级硬上限建议 **CNY 0.50**（需书面确认） |
| 凭据 | 环境变量 `GLM_API_KEY`（已解析），不显示值 |
| 证据 | `artifacts/phase15/real-inference-<timestamp>.json`：HTTP 状态、model_echo、usage、cost、latency、脱敏错误；扫描 API 响应/事件/artifact/导出确认无 Key |

只有上述单次推理成功且计费对账一致后，才讨论一次受 CNY 3.00 硬上限约束的 Live M2 E2E
（Docker Sandbox 必需，不得启用模拟沙箱逃生门）。

---

## 7. M3 计划（P15-05）

见 [`docs/notes/phase15-m3-execution-plan.md`](phase15-m3-execution-plan.md)。本阶段只完成计划：

- L1 `pvlib__pvlib-python-1072`（official dev，未在历史 GLM 运行中出现）；
- L2 10 题 dev 校准清单已按 repo 分层固定，但 `cal10.json` 尚未创建/冻结；
- L3 test50 冻结清单与 hash 复用 `configs/subsets/`；
- 归档、官方/内部双口径、去重、重试分类、费用公式与每层门禁均已写明；
- M3/M4/test50/test300 **未执行**，需独立授权。

---

## 8. 定向验证记录

### 8.1 后端

```text
uv run pytest tests/unit/settings/test_phase14_settings.py \
  tests/unit/settings/test_phase15_routing_matrix.py \
  tests/api/test_phase14_settings_api.py \
  tests/unit/llm/test_tracking_chain_accounting.py \
  tests/unit/llm/test_dispatch_ledger_reconciliation.py \
  tests/unit/llm/test_dispatch_budget.py \
  tests/unit/llm/test_phase14_protocols.py -q --no-cov
# 65 passed in 84.88s，exit 0
```

包含：GLM14 重复记账回归、派发账本对账、设置验证状态持久化/过期/失败、路由矩阵、
Anthropic 原生 M2 E2E、Bedrock fake client、协议错误映射、SSRF 负控制。

### 8.2 前端

```text
cd web
npm test -- --run src/lib/stages.test.ts src/components/SettingsView.test.tsx \
  src/i18n/i18n.test.ts
# 3 files / 18 tests passed，exit 0（43.29s）
npm run lint
npm run typecheck
npm run build
# 全部通过；主包 427.57 KB / gzip 131.98 KB
```

### 8.3 静态检查

```text
uv run ruff check <本次修改的 6 个 Python 文件>
uv run ruff format --check <同上>
uv run mypy src/codepilot/settings/store.py src/codepilot/settings/service.py \
  src/codepilot/llm/dispatch_budget.py
# All checks passed / 6 files already formatted / Success
```

前端 ESLint 0 warning；TypeScript `tsc -b` 无错误；Vite build 成功。

### 8.4 浏览器

`scripts/capture_phase15_screenshots.mjs` 使用真实 Chrome headless，1440×900（窄桌面检查
1100×800），访问隔离的本地 API + Vite：

- 中文 Dark 新建任务、Provider 设置、执行会话；
- English Light 新建任务、通用设置、历史回放；
- 刷新后语言/主题保持；
- System 主题跟随 OS 暗色，并在运行中切换到亮色；
- 长 Provider/长模型 ID、操作菜单可达、页面无横向溢出；
- Provider 搜索无结果时显示明确空状态；对缺少凭据的 OpenAI 模板执行本地校验时，弹窗显示
  「配置需要处理」及逐项原因（未发起网络请求）；
- 时间线「独立审查」阶段数为 1。

截图位于 `docs/assets/phase15/01..10`，对应本阶段提交的界面代码；数据来自隔离设置与离线
演示任务，**没有任何真实推理验证记录**。

### 8.5 未执行

- 全量 `pytest -m "not docker and not llm"`：本轮只改 Settings/UI/Timeline/测试，未改全仓
  契约；Phase 14 基线 3584 passed / 8 skipped 保留为历史读数，不宣称本轮重跑。
- Docker 测试集、Compose 全链路：未改 Sandbox/Compose；且 Docker 命名管道当前不可用。
- 779 条基线、真实 SWE-bench 批跑、M4、test50/300、GitHub Actions：NOT RUN。
- 真实 Provider 推理、Live M2 E2E：NOT RUN（无费用授权；Docker BLOCKED）。

---

## 9. 已知限制与下一步

1. Docker Desktop 命名管道对 Python/Docker SDK 拒绝访问（Git Bash 直连 CLI 成功，说明是
   会话通道差异）；Worker 依赖 SDK，故记 BLOCKED。需在 Docker Desktop 侧修复/重启后再跑
   `--dry-run` 与 Live E2E。
2. 当前 Settings 库没有真实推理记录；界面显示「未测试」是事实状态，不是失败率。
3. 连接测试指纹不包含环境变量的**值**；环境变量值在外部被替换时无法检测，需重新测试。
   界面/文档已说明这一点，未伪造验证。
4. L2 的 `swebench-lite-cal10.json` 尚未冻结；在创建并记录 hash 前不得启动 L2。
5. 预检脚本只读设置库；候选模板使用临时 DB，不会把候选供应商写入实际设置。
6. 本地提交包含 Phase 13–14 的历史工作树内容；未包含 `.env`、运行 artifact、临时目录或
   `.playwright-mcp/`。
