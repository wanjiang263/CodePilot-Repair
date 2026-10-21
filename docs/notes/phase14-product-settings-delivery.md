# Phase 14：产品设置系统、多模型接入与工程化交付

日期：2026-10-21。分支 `codex/phase12-real-issue-pilot`，开工 HEAD `b4effac`。
本轮保留工作树、历史 artifact 与只读回放目录；未自动 commit / push / merge / reset / stash，
未调用任何付费模型，未运行真实 SWE-bench 批跑，未重启 Docker Desktop，未操作其他项目容器。

---

## 1. 交付状态

| 任务 | 状态 | 说明 |
|---|---|---|
| P14-01 中英国际化 | **DONE** | react-i18next；zh-CN/en-US 全量资源；浏览器默认 + 服务端偏好；切换即时生效 |
| P14-02 明暗/System 主题 | **DONE** | CSS tokens；Light/Dark/System；首屏无闪烁；Diff/代码/日志/报告独立配色 |
| P14-03 Provider 管理 | **DONE**（Bedrock 为 PARTIAL） | 内置目录、自定义、凭据、模型、发现、三档连接测试；Anthropic 原生协议已实现；Bedrock 代码与 Mock 完成，真实 AWS 未验证且 boto3 未安装 |
| P14-04 路由与真实运行配置 | **DONE**（真实付费 NOT RUN） | 角色覆盖、任务覆盖、快照冻结、预算准入、MockTransport 全图路由 |
| P14-05 设置中心 | **DONE** | 8 个分区；加载/空/错误/版本冲突反馈；无假开关 |
| P14-06 Settings 后端 | **DONE** | 类型化文档、版本控制、审计、导出/导入、脱敏 API |
| P14-07 Docker Compose | **DONE**（见 §6 实际结果） | API + Worker + 静态前端 + SQLite；健康检查；非 root；回环绑定；持久卷 |
| P14-08 文档与展示 | **DONE**；视频 NOT RUN | README、架构/API、安全、project-summary、6 张真实浏览器截图、演示脚本 |
| P14-09 验证 | **DONE**（真实付费/批量评测 NOT RUN） | 见 §7 |
| 官方 SWE-bench M3 / 消融 M4 / test50/300 / 10 题校准 / 真实 GitHub Actions | **NOT RUN** | 不因 Phase 14 UI/部署完成而自动完成 |

---

## 2. 配置数据模型

### 2.1 设置文档

`SettingsDocument`（`src/codepilot/settings/models.py`）：

- `version`：单调递增，PUT 必须携带期望版本；冲突返回 409，不静默覆盖。
- `general`：语言、主题、默认执行模式、推理内容展示、活动信息密度。
- `agents`：全局默认 provider/model + 6 个真实可路由角色覆盖。
- `execution`：预算默认值、币种、汇率、最大并行 live 任务。
- `workspace`：默认仓库类型/值（只用于预填，不是授权边界）。
- `providers`：ProviderConfig 字典。
- 设置持久化在 `artifacts/settings/settings.sqlite3`（Compose 为 `/data/artifacts/settings/...`）。

### 2.2 Provider / Model / 凭据

`ProviderConfig`：

```text
provider_id (immutable, unique)
name, protocol (openai_compatible | anthropic_messages | bedrock_converse)
base_url, enabled, custom, catalog_id
credential { kind: none|env|vault, name, updated_at }
timeout_seconds, max_retries, max_concurrency
region, aws_profile
models: ModelConfig[]
```

`ModelConfig` 包含 `model_id/display_name/context_window/max_output_tokens/`
`supports_tools/supports_json_mode/supports_temperature/supports_thinking/`
`price {currency, input/output/cached per Mtok}/source/source_note/favorite`。

凭据：

- `env`：只保存环境变量名；Worker 读取环境或 `.env`。
- `vault`：API 立即用 Fernet 加密到独立文件；视图只返回 `kind/available/updated_at`。
- 任务快照保存凭据引用，不保存明文。
- 导出不包含明文；导入拒绝 Vault 引用。

### 2.3 任务快照与生效优先级

创建任务时在 `api_tasks.config_json/config_hash` 冻结：

- mode（fake/live）、默认 provider/model、每个角色的 provider/model/reasoning；
- 协议、Base URL、超时/重试/并发、region/profile；
- 模型能力、上下文/输出上限、价格、预算币种与汇率；
- 凭据引用（环境变量名或 Vault id）。

优先级：

```text
任务级 model override > Agent role override > Global default
```

CLI/YAML 路径不受 Web 设置影响；Web 任务的 `execution_mode=live` 与 CLI 评测参数各自独立。

### 2.4 冻结语义

- 修改 Settings 只影响之后创建的任务。
- 运行中的任务继续使用自身快照；预算不会因设置变化而重置。
- 凭据撤销后，恢复执行会 fail closed（`credential_missing`），不会回退为无鉴权。

---

## 3. 供应商与协议

| 供应商 | 协议 | 已接入 | 已验证 | 备注 |
|---|---|---|---|---|
| OpenAI | OpenAI Chat Completions | 是 | MockTransport；无真实付费调用 | 模板模型规格需核实 |
| Anthropic | 原生 Messages | 是 | MockTransport：文本、tool_use、usage/cost、401 脱敏 | 未调用真实 Anthropic API |
| DeepSeek | OpenAI 兼容 | 是 | 历史真实 GLM 路径外，规格来自 `configs/llm.yaml` | 模板价格带来源 |
| Z.AI | OpenAI 兼容 | 是 | 规格来自既有配置 | USD 价格需汇率 |
| BigModel | OpenAI 兼容 | 是 | 复用 Phase 11 已验证 GLM Provider | 模板价格带来源 |
| Moonshot / Kimi | OpenAI 兼容 | 是 | 规格来自既有配置 | — |
| 阿里云百炼 / Qwen | OpenAI 兼容 | 是（模板） | 仅连接测试代码路径；无真实调用 | 模型/价格未内置，需手动填写 |
| AWS Bedrock | Converse + AWS SDK | 代码路径 | 确定性 fake client | 真实 AWS 未验证；需 `uv sync --extra bedrock` |
| Custom | OpenAI 兼容 / Anthropic | 是 | MockTransport、SSRF 负控制 | 仅 HTTPS 公网端点 |

**没有**把 OpenAI 兼容适配器冒充 Anthropic/Bedrock 的原生能力。Bedrock 不使用 API Key
冒充 IAM；未安装 SDK 时目录明确不可用。

连接测试三档严格区分：

1. **配置校验**：纯本地，不发起网络请求。
2. **模型发现**：用户点击才请求供应商目录；不自动保存结果。
3. **真实推理**：用户确认后发送一次最多 16 输出 token 的请求；不启动 Coding Agent。

所有测试有超时、响应大小上限、脱敏与 SSRF 保护；真实推理的 Tool Calling 只报告
`declared_not_verified`，不谎称已验证。

---

## 4. Settings API

全部端点位于 `/api/v1`，统一要求 `X-API-Key`。

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/settings` | 读取文档 + 脱敏 Provider 视图 + 版本 |
| PUT | `/settings/sections/{section}` | 更新 general/agents/execution/workspace |
| GET | `/settings/catalog` | 内置供应商目录 |
| GET | `/settings/providers` | 脱敏 Provider 列表 |
| POST | `/settings/providers` | 创建自定义/完整 Provider |
| POST | `/settings/providers/from-template` | 从内置模板创建 |
| PUT / DELETE | `/settings/providers/{id}` | 编辑 / 删除（被路由引用时拒绝） |
| POST / DELETE | `/settings/providers/{id}/credential` | 保存/撤销凭据 |
| POST / PUT / DELETE | `/settings/providers/{id}/models[/{model_id}]` | 模型 CRUD |
| GET | `/settings/models` | 可用模型 + ready/blockers |
| POST | `/settings/providers/{id}/test` | config / models / inference |
| POST | `/settings/providers/{id}/discover` | 主动模型发现 |
| GET | `/settings/export` | 无明文配置导出 |
| POST | `/settings/import` | 带版本导入 |
| GET | `/settings/audit` | 设置审计 |
| GET | `/settings/logs` | 最近事件摘要（脱敏） |
| GET | `/settings/docs/{name}` | 鉴权文档读取（白名单） |
| GET | `/settings/runtime` | Worker/Docker/EventSink/Checkpointer/存储/诊断 |
| POST | `/settings/cleanup` | 已完成用户任务 + run artifact 清理（需 `DELETE`） |

任务接口扩展：

- `POST /tasks` 支持 `execution_mode=live`、`model {provider_id, model_id}`；
- live 必须提供 `budget.max_cost > 0`，且不超过服务端 `CODEPILOT_API_MAX_LIVE_COST`；
- 响应新增 `llm_mode/provider_id/model_id/model_identity/config_hash`；
- `GET /tasks/{id}`、`GET /runs/{id}/report` 返回冻结身份与配置 hash。

---

## 5. 后端与前端改动

新增：

- `src/codepilot/settings/`：`models.py`、`store.py`、`credentials.py`、`catalog.py`、
  `security.py`、`service.py`。
- `src/codepilot/llm/anthropic_messages.py`、`bedrock_converse.py`。
- `src/codepilot/api/settings_routes.py`。
- `web/src/i18n/`、`web/src/locales/{zh-CN,en-US}/`、`web/src/hooks/useSettings.ts`。
- `web/src/components/settings/*`（General/Provider/Agents/Permissions/Budget/Workspace/
  Advanced/About/shared）。
- `Dockerfile`、`docker-compose.yml`、`.dockerignore`、`scripts/capture_screenshots.mjs`。
- 文档：`docs/06-security.md`、`docs/project-summary.md`、本文件、演示脚本、截图。

关键修改：

- `llm/config.py`：ProviderSpec 增加 `protocol/region/aws_profile/api_key_value`（运行期密钥
  不参与 dump）。
- `llm/registry.py`：按协议选择 OpenAI/Anthropic/Bedrock 适配器；保留既有 OpenAI 路径。
- `eval/assembly.py`：`ProviderBundle.live` 支持注入 client factory，供 MockTransport 与
  SSRF 客户端使用。
- `api/app.py`：接入 SettingsService/SettingsRouter；版本冲突与设置错误信封。
- `api/database.py`：任务配置列、队列统计、活跃 live 计数、已完成任务删除、最近事件。
- `api/worker.py`：live 快照执行、Docker/模拟沙箱准入、Provider 关闭、健康心跳。
- `api/routes.py`：live 准入、meta 的 live 模型/原因码、默认预算、冻结身份。
- `repo/workspace.py`：Windows + Git for Windows 的 MSYS local transport 在部分会话中
  无法启动 `sh`（`NtCreateDirectoryObject`）。新增仅在本地源出现该错误时的
  “bare 缓存复制 + 仅复制 base 可达对象”回退；远程克隆路径不变，且回退仍保证
  base 之后的提交不会进入 workspace 对象库（新增专门测试）。
- 前端全部主组件接入 i18n；主题增加 System；Composer 增加执行模式/模型/费用硬上限；
  RunPanel 展示冻结身份与 config hash。

---

## 6. Docker Compose 实际结果

**DONE**。使用项目名 `codepilot-phase14` 构建并启动；只管理本项目容器/网络/数据卷。

- `docker compose -p codepilot-phase14 up -d --build`：镜像构建成功，API 与 Worker 均
  `healthy`。
- API 鉴权：无 `X-API-Key` 返回 401；带正确 key 的 `/health` 与 `/settings` 返回 200。
- `http://127.0.0.1:8000/app/`：真实浏览器可打开，中文界面、离线模式与本地 API 连接正常。
- Worker 执行离线演示任务：`completed`，`/runs/<id>/report` 与 `/runs/<id>/patch`
  都返回 200；任务完成后 `last_worker` 可见（修复了旧逻辑清空 worker_id 的诊断盲点）。
- 重启 API 与 Worker 后，任务、报告、设置（version=1, en-US/dark）仍可读取。
- Docker 连通性诊断：`docker.available=true`。
- `/settings/docs/security`：200（文档通过鉴权 API 读取，不作为未鉴权静态文件）。
- 清理接口：输入 `DELETE` 后删除指定已完成用户任务；另一个已完成任务仍返回 200，
  被删任务返回 404；清理只作用于本项目测试数据。
- 停止：`docker compose -p codepilot-phase14 down` 仅移除本项目容器与网络；重启恢复验证完成
  后，测试数据卷 `codepilot-repair-phase14-data` 与本项目测试镜像也已删除，未留下本项目
  测试容器/网络/卷；镜像可用 `docker compose build` 重建。
- Docker socket 挂载给 API/Worker 的风险写入 `docs/06-security.md`；没有使用
  `privileged: true` 或放宽隔离来强行让测试通过。


---

## 7. 验证记录

### 7.1 浏览器实测（Chrome headless，1440×900，真实 API）

- 中文（系统默认）与 English 切换即时生效，刷新后保留；服务端 `general.language`
  同步（真实设置版本 0→1）。
- Light/Dark 切换即时生效；`documentElement.dark`、`dataset.theme`、`localStorage` 与服务端
  `general.theme` 同步（版本 1→2）。
- 从内置目录创建 DeepSeek Provider（版本 2→3），模型 `deepseek-flash` 显示 ready；
  凭据只显示环境变量来源；设为默认模型（版本 3→4）；为 Localizer 设置角色覆盖（版本 4→5）；
  修改最大迭代次数为 6（版本 5→6）。
- 离线任务通过 UI 创建，Worker 完成后时间线、Diff、测试/审查/报告可读。
- 截图：`docs/assets/phase14/01..06`。
- 未录制视频：**NOT RUN**（演示脚本已交付）。

### 7.2 自动化

最终门禁结果（2026-10-21，WSL2 Ubuntu 24.04 / Python 3.12.3；代码与 Windows 工作树同一份）：

- 后端 `ruff check .`、`ruff format --check .`、`mypy src`：通过。
- 后端 `pytest -m "not docker and not llm" --cov`：**3584 passed, 8 skipped, 50 deselected**，
  exit 0；**覆盖率 88.59%**（阈值 80%）。
- API 定向测试：Phase 13 既有 106 项与 Phase 14 Settings/凭据/路由/清理用例全部通过。
- 前端 `npm test`：37 passed；`npm run lint`、`npm run typecheck`、`npm run build`：通过。
- Provider/路由 E2E：MockTransport 下 alpha/beta 两个 Provider 分别承接 localizer 与
  planner/coder/reviewer；配置快照在设置修改后保持不变。
- 历史回放：glm11～glm14 只读，官方/内部判定分离。
- Compose smoke：见 §6。

说明：Windows 原生 Git Bash 会话在本机存在两项环境限制——Python 派生的 MSYS `sh`
无法创建 `BaseNamedObjects` 命名对象，以及当前用户缺少创建符号链接的权限。直接在该会话
跑全量测试会有 29 个环境相关失败（工作区 transport、symlink、Git alias 测试）。同样的代码
在 WSL Linux 全量门禁通过；Phase 14 为本地 Git transport 增加了只针对该 MSYS 启动错误的
回退（仍只复制 base 可达对象），但没有降低测试门槛或跳过任何失败用例。Windows 定向测试
（Settings、协议、MockTransport、E2E API）已通过。

### 7.3 安全负控制

- 明文 Key 不出现在：设置 API 响应、任务快照、`api_events`、artifact、导出 JSON。
- Vault 文件只包含 Fernet 密文；撤销后文件删除。
- `http://`、loopback、私有地址、重定向、超限响应、Vault 未知引用均被拒绝。
- 未配置费用上限/价格/汇率时不创建 live 任务。
- 清理接口只处理已完成的用户任务；历史 benchmark 不受影响。

---

## 8. 未完成与限制

- 未调用任何付费 Provider；**本轮 live 的真实付费运行 NOT RUN**。
- AWS Bedrock 真实调用未验证；boto3 不是默认依赖。
- Anthropic 真实调用未验证。
- 官方 SWE-bench M3、M4 消融、test50/test300、10 题费用校准、真实 GitHub Actions CI：
  **NOT RUN**，不得标为完成。
- 演示视频 NOT RUN。
- 单机单用户；无多租户/RBAC/配额；Docker socket 高权限；沙箱不覆盖恶意 Docker API。
- DA-022 真实同形状仍 `NOT OBSERVED`；不因本轮界面或部署完成而改变。
- 前端主 bundle 约 513 kB（gzip 148 kB），有 Vite chunk 警告；未做代码分割，不影响本轮验收。

### 7.4 证据位置

- Linux 全量门禁日志：`artifacts/phase14-pytest-linux-final.log`
- 前端/typecheck/build 日志：本次会话输出；`web/dist/` 为构建产物
- Compose 构建日志：`artifacts/phase14-compose-final6-build.log`
- 浏览器截图：`docs/assets/phase14/`
- 真实历史回放：`configs/benchmarks.yaml` + `artifacts/phase11-glm1{1,2,3,4}-20261020/`
