# 安全模型（Phase 14 / Phase 15）

日期：2026-10-22。本文描述的是当前代码实际执行的边界，而不是路线图承诺。任何与代码不一致的描述都应视为缺陷。

---

## 1. 两个互不相干的信任域

| 凭据 | 用途 | 存放位置 | 是否会被发给浏览器 |
|---|---|---|---|
| `CODEPILOT_API_KEY` | Dashboard/API 的 `X-API-Key` | 服务端环境变量；浏览器只保存在当前标签页的 `sessionStorage` | 是（仅用于同源 API 请求） |
| Provider API Key | 服务端 Worker 调用模型 | 环境变量引用或加密凭据库 | **否** |

两者不能互相替代：

- 模型 Key 永远不会被返回给浏览器，也不会写进 `localStorage`、`sessionStorage`、SSE 事件、`api_events`、run artifact、模型请求记录或配置导出。
- `CODEPILOT_API_KEY` 不是模型凭据，只授予本地 API 的调用权。
- 前端不会直连模型服务商；所有 live 请求都由独立 Worker 发出。

## 2. Provider 凭据存储

- 明文形式只接受两种输入：
  1. **环境变量引用**：设置里只保存变量名（如 `DEEPSEEK_API_KEY`），Worker 在派发时读取进程环境或 `.env`。
  2. **加密凭据库**：API 收到一次 API Key 后立刻用 Fernet 加密写入 `artifacts/settings/credentials/<id>.cred`，只返回凭据元数据（`kind`、是否可用、更新时间）。之后 API 不再返回明文。
- 主密钥来源：`CODEPILOT_SETTINGS_MASTER_KEY`，或设置目录内自动生成的 `master.key`（尽量设为 0600）。
- 加密后端不可用时，`vault.put` 抛错，界面只能选择环境变量引用；**不会静默降级为明文**。
- 主密钥文件位于运行数据卷，`.gitignore` 已忽略；不要把 `artifacts/` 或 `.env` 提交到 Git。
- 撤销凭据会删除对应密文文件；已冻结的任务只保存凭据引用，撤销后恢复运行会 fail closed。
- Worker 执行前还会核对快照中的 `credential_kind/name` 与**当前**设置引用是否一致。撤销或
  替换引用后，旧任务返回 `credential_missing` / `credential_changed`，即使旧环境变量仍然
  存在、或新凭据已经可用，也不会静默沿用旧引用。
- 任务快照只包含 `credential_kind` 与 `credential_name`（变量名/随机引用 id），不含 Key 值。
- 所有设置变更写入 `settings_audit`（动作、目标、摘要、时间），不含凭据值。

## 3. 自定义端点与 SSRF

设置中心允许用户添加自定义 Base URL，因此请求层必须把“用户可控 URL”当作不可信输入：

- 只允许 `https://`；拒绝 URL 中的用户名/密码、查询串和片段。
- 拒绝 `localhost`、回环、私有、链路本地、保留地址与元数据地址（含 IPv4/IPv6 字面量）。
- 请求时重新解析 DNS，并要求**所有**解析结果都是全局可路由地址；随后把 TCP 连接固定到已验证的 IP。
- 保留原始 `Host` 头，并通过 `sni_hostname` 让 TLS 证书仍按原域名校验，避免“改成 IP 后证书失效”。
- 关闭自动重定向；3xx 直接拒绝。
- 自定义端点使用 `trust_env=False` 的客户端，避免环境代理绕过上述校验。
- 连接测试和模型发现都有超时、响应体上限（256 KiB）和失败脱敏。
- DNS 重绑定：校验与连接发生在同一请求路径，连接目标是**本次解析并验证过的 IP**，因此重绑定不能把连接改到内网地址。

已知限制：

- 内置目录中的服务商使用固定官方域名，但若部署环境强制要求 HTTP 代理，自定义端点不会使用该代理。这是安全优先的取舍。
- 若攻击者能修改容器内 `/etc/hosts` 或控制 DNS 服务器，仍属于宿主/DNS 层攻击，不在应用层威胁模型内。

## 4. Workspace 边界

- 浏览器不能传文件路径；API 只接受仓库 URL/演示仓库名，并受 `CODEPILOT_API_ALLOWED_REPOSITORY_HOSTS` 白名单约束。
- 所有文件工具在 `WorkspacePathPolicy` 下运行，拒绝 `../`、绝对路径、符号链接逃逸和评价夹具目录。
- 每个 run 使用独立工作区与 artifact 目录；API 只按数据库里的 task/run 身份解析产物，不接受前端路径。
- 清理接口只删除**已完成的用户任务**及其 `artifacts/runs/<run_id>`；历史 benchmark 目录、只读回放包和运行中任务不在删除范围。删除需要输入 `DELETE` 并在服务端再次校验版本与任务状态。

## 5. Docker socket

Compose 把 `/var/run/docker.sock` 挂载给 API 与 Worker，用途是：

- Worker 使用 `DockerSandbox` 构建/运行仓库测试环境；
- API 的“高级”页做只读 Docker 连通性诊断。

风险：

- Docker socket 等价于宿主 root 级能力。任何能调用 API 并影响容器参数的人，理论上都可以越过“仓库工作区”边界。当前部署默认只绑定 `127.0.0.1`，面向单用户本地使用。
- 不要把本 Compose 暴露到公网，也不要把 API Key 交给不受信任的用户。
- 若需要多租户或远程团队使用，必须把 Docker 执行迁移到隔离的远程 runner（独立主机/VM、最小权限 socket 代理或 rootless 容器运行时），并引入每用户授权与资源配额。
- `no-new-privileges` 阻止容器内提权，但不能限制通过 Docker API 创建的新容器。

这与既有沙箱的信任边界一致：沙箱保护的是“Agent 修改宿主机/其他项目文件”的常规路径，不是“恶意 Docker API 调用者”。

## 6. 沙箱能保护什么、不能保护什么

能覆盖：

- 仓库测试在独立容器/工作区内执行；
- 构建与测试超时、输出尾部截断、测试次数预算；
- 仓库路径白名单与只读/写入权限分离；
- Agent 工具调用的参数 schema 与工作区路径校验；
- 取消只在节点边界生效，不杀 Docker daemon、不影响其他项目容器。

不能覆盖：

- 恶意依赖在构建/测试期间对容器内核/宿主 Docker 的攻击；
- 模型输出中的社会工程内容；
- 用户自己通过设置中心添加的恶意 Provider（可读取发往它的仓库内容）；
- 宿主级 root、Docker daemon 或内核已被攻陷的情况；
- 多租户隔离、跨用户资源配额、计费结算。

## 7. Live 运行准入与费用

- 服务端必须显式允许（`CODEPILOT_API_ALLOW_LIVE_LLM=true`），且默认要求 Docker 沙箱后端。
- 用户必须显式选择 live 模式；每个 live 任务必须提供正的费用硬上限，且不得超过服务端 `CODEPILOT_API_MAX_LIVE_COST`。
- 路由涉及的所有模型都必须有完整的输入/输出价格，并且价格币种能换算到预算币种；否则在入队前拒绝（fail closed）。
- 模型 ID、上下文窗口、输出上限、工具调用能力缺失时，不允许 live 路由。
- 任务创建时冻结 provider/model/协议/角色路由/配置 hash/预算；运行中的任务不会因设置修改而换模型或重置预算。
- 凭据在任务创建时只冻结引用；Worker 在执行时先核对当前设置中的引用仍与快照一致，再解析
  值。凭据被撤销/替换时任务失败，而不是回退到无鉴权、沿用旧引用或免费假设。
- 连接测试的“真实推理”只发送一次极小请求（最多 16 个输出 token），必须由用户显式确认，不会启动完整 Coding Agent。
- 本阶段没有调用任何付费 API，也没有运行真实 SWE-bench 批跑。

## 8. 单用户本地部署 vs 生产多租户

当前产品定位是**单用户、本地/受信网络**的 Agent 工作台：

- 一个 API Key 代表一个操作员；
- SQLite 队列 + 单 Worker；
- 没有租户、配额、审计导出或 SSO；
- 设置是全局的，不是每用户设置；
- Docker socket 与运行产物都在同一宿主。

生产多租户需要额外设计：身份与 RBAC、每用户工作区与产物隔离、每租户预算/并发配额、远程沙箱 runner、密钥保险库（如 KMS/Vault）、审计留存、数据保留与删除策略。这些**不在本轮交付范围内**。

## 9. 报告与导出

- 设置导出只包含非敏感配置与凭据引用（环境变量名）；Vault 引用在导出中只保留 `kind`，导入时要求重新录入。
- 运行时日志页面只展示最近的事件摘要，经过 `PathScrubber` 脱敏（绝对路径、密钥模式）。
- 官方 SWE-bench verdict 与内部验证级别始终分开记录；安全或部署状态不会推导评测结论。
