# LLM 配置与代理

模型 ID、服务地址、能力与价格统一放在 `configs/llm.yaml`；密钥值只放环境变量或
已忽略的 `.env`。`roles` 引用项目别名，因此换模型时保留别名、修改 `model` 即可。

## 官方资料与估算口径

以下配置在 2026-10-09 核实。单价单位均为人民币 / 百万 token，列顺序为
普通输入、输出、缓存命中输入。

| 项目别名 | 官方模型 ID | 配置单价 | 官方来源 |
|---|---|---|---|
| `deepseek-chat` | `deepseek-flash` | 2 / 8 / 0.04 | [模型与价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/) |
| `kimi-main` | `kimi-k2.6` | 6.5 / 27 / 1.1 | [平台价格](https://platform.kimi.com/)、[模型参数](https://platform.kimi.com/docs/guide/kimi-k2-6-quickstart) |
| `glm-main` | `glm-4.7` | 4 / 16 / 0.8 | [模型说明](https://docs.bigmodel.cn/cn/guide/models/text/glm-4.7)、[计价表](https://docs.bigmodel.cn/cn/guide/start/pricing) |

DeepSeek 使用高峰价格；GLM 使用长输入最高档价格。这是按核实当日公开价格作出的
保守预算估算，未实现分时、分档自动选择，也不代表厂商实际扣费。账单以厂商控制台为准，
价格变化后需更新 YAML。上下文窗口按文档的 M/K 数值保守配置，输出预算仍为 4096。

三个模型默认关闭思考模式。开关来自模型的 `extra_body`，由同一个兼容客户端发送，
无需添加厂商分支。依据：[DeepSeek 思考模式](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/)、
[Kimi 参数](https://platform.kimi.com/docs/guide/kimi-k2-6-quickstart)、
[GLM 思考模式](https://docs.bigmodel.cn/cn/guide/capabilities/thinking-mode)。当前 Agent 消息协议
不回传思考内容；如需开启思考并使用工具，应先核对厂商对历史思考内容回传的要求。
Kimi 温度为模式固定值，因此配置 `supports_temperature: false`，省略请求温度参数。

`extra_body` 不能覆盖 `model`、`messages`、`max_tokens`、`temperature`、`tools`、
`tool_choice`、`response_format`、`stream`。这些字段由请求与能力标志管理，加载时会拒绝冲突。

## 检查与连通性验证

```bash
uv sync --extra dev --locked
uv run codepilot config check
uv run python scripts/llm_smoke.py deepseek
```

冒烟脚本会确认费用后发起真实请求；`--yes` 可跳过交互确认。它只验证普通文本对话，
不会发送仓库源码。空回复或截断回复判定为失败，打印费用明确标为“按配置估算”。
结构化输出与工具调用的真实验收需另外执行，不能由普通对话成功推断。

未配置 Kimi、GLM 或 OpenAI 密钥时，完整性检查仍会列出这些服务；这不影响选择已有
DeepSeek 密钥的普通对话冒烟。`--strict` 要求所有声明的服务与角色配置完整。

## 本机 Git 与 HTTP 代理

本次移除全局 Git 配置中的失效 `http.proxy`、`https.proxy`（7890 端口），保留其他配置。
Git 使用已有 `HTTP_PROXY`、`HTTPS_PROXY` 环境变量，不再依赖一次性命令覆盖。
环境变量中的端口属于本机设置，不写入项目源码。

项目依赖使用 `httpx[socks]`，使已有 `ALL_PROXY` 的 SOCKS 代理配置可以正常初始化客户端。
切换代理软件端口时同步更新环境变量，并在新的终端进程中验证连接。

## 2026-10-09 本机验证

| 验证 | 实际结果 |
|---|---|
| Git 读取 `origin/main`，无代理覆盖 | 成功，远端 SHA 为 `40c96df6426cdf75532032d0d3af4b271c10e4fa` |
| `codepilot config check` | 无模型与价格缺口；仍缺 GLM、Kimi、OpenAI 密钥 |
| `python scripts/llm_smoke.py deepseek --yes` | 普通对话 1/1 成功，返回“收到”；9 输入 / 1 输出 token，按配置估算 0.000026 CNY |
| 针对性测试 | 87 passed，覆盖 YAML 修改生效、参数冲突、参数透传、冒烟结果判定与 CLI 严格检查 |
| `ruff check .` | PASS |
| `ruff format --check .` | PASS，98 files already formatted |
| `mypy src` | PASS，63 source files |
| `pytest -m "not docker and not llm"` | 699 passed / 3 skipped，318.85 秒；跳过项均因 Windows 符号链接权限不足 |

本机使用 `.venv/Scripts/` 中的工具；测试临时目录独立创建。新增 19 项单元测试，
未放宽检查、排除源码或修改已有用例的失败退出码。完整输出与 Git 配置备份位于
`artifacts/git-llm-config/20261009-164350/`，按项目规则不提交。

GLM/Kimi 真实调用、结构化输出与工具调用真实验收、Docker、此次改动的 Linux CI
均为 **NOT RUN**。普通对话成功不能视作 L-18 完整三路径验收通过。
`.env` 仍被忽略且未跟踪，改动文件与验证日志的密钥扫描均为 0 匹配。

### 符号链接权限修复（2026-10-09 后续验证）

经用户明确授权，已开启 Windows 开发者模式：
`HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock\AllowDevelopmentWithoutDevLicense = 1`。
普通用户进程（非管理员）已实际创建文件、目录和自循环符号链接。

重跑 `pytest tests/unit/repo/test_scanner.py -ra`：**55 passed / 0 skipped，51.48 秒**。
JUnit 结果确认此前因 WinError 1314 跳过的三项均已执行通过；未修改测试的跳过条件或断言。
本次测试在 Codex 文件系统沙箱外以普通用户身份运行；沙箱自身的文件写入限制另行生效。
上表的全量结果保留为修复前历史证据，本次未重跑全量测试。
原设置备份、真实链接探测和测试结果位于 `artifacts/symlink-permissions/20261009-172246/`。
