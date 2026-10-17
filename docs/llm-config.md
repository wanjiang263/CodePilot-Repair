# LLM 配置与代理

模型 ID、服务地址、能力与价格统一放在 `configs/llm.yaml`；密钥值只放环境变量或
已忽略的 `.env`。`roles` 引用项目别名，因此换模型时保留别名、修改 `model` 即可。

## 官方资料与估算口径

DeepSeek / Kimi 于 2026-10-09 核实；GLM 于 2026-10-17 核实，能力差异与真实 API 冒烟结论见
[notes/glm-integration.md](notes/glm-integration.md)。单价单位均为**每百万 token**，
列顺序为普通输入、输出、缓存命中输入。币种是每个模型自己的 `price.currency`：
BigModel 站以 CNY 计价、Z.AI 站以 USD 计价，**两种币种不能混记进同一张预算表**。

| 项目别名 | provider | 官方模型 ID | 币种 | 配置单价 | 官方来源 |
|---|---|---|---|---|---|
| `deepseek-chat` | deepseek | `deepseek-flash` | CNY | 2 / 8 / 0.04 | [模型与价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/) |
| `kimi-main` | kimi | `kimi-k2.6` | CNY | 6.5 / 27 / 1.1 | [平台价格](https://platform.kimi.com/)、[模型参数](https://platform.kimi.com/docs/guide/kimi-k2-6-quickstart) |
| `glm-main` | bigmodel | `glm-4.7` | CNY | 4 / 16 / 0.8 | [计价表](https://docs.bigmodel.cn/cn/guide/start/pricing) |
| `glm-5-main` | bigmodel | `glm-5` | CNY | 6 / 22 / 1.5 | 同上 |
| `glm-5.1-main` | bigmodel | `glm-5.1` | CNY | 8 / 28 / 2 | 同上 |
| `glm-5.3-main` | bigmodel | `glm-5.3` | CNY | 8 / 28 / 2 | 同上 |
| `glm-4.7-zai` | zai | `glm-4.7` | USD | 0.6 / 2.2 / 0.11 | [Z.AI 定价](https://docs.z.ai/guides/overview/pricing) |
| `glm-5-zai` | zai | `glm-5` | USD | 1.0 / 3.2 / 0.2 | 同上 |
| `glm-5.1-zai` | zai | `glm-5.1` | USD | 1.4 / 4.4 / 0.26 | 同上 |
| `glm-5.3-zai` | zai | `glm-5.3` | USD | 1.4 / 4.4 / 0.26 | 同上 |

DeepSeek 使用高峰价格；BigModel 的 GLM 按**输入长度分档**（`glm-4.7` 还按输出长度分档），
一律取公开最高档；Z.AI 不分档。这些都是按核实当日公开价格作出的保守预算估算，
未实现分时/分档自动选择，也不代表厂商实际扣费；缓存存储费与 Batch API 折扣未建模。
账单以厂商控制台为准，价格变化后需更新 YAML。上下文窗口按文档的 M/K 数值保守配置，
请求侧输出预算仍为 4096。

思考模式：**不是所有模型都能关**。`glm-4.7`、`deepseek-chat`、`kimi-main` 通过
`extra_body: {thinking: {type: disabled}}` 关闭，由同一个兼容客户端发送，无需厂商分支；
`glm-5` / `glm-5.1` 思考无法关闭，因此别名里根本不下发该参数，并用
`supports_thinking: false` 声明（此时再往 `extra_body` 里写 `thinking` 会在加载期报错）。
当前 Agent 消息协议不回传思考内容；2026-10-17 的真实冒烟证实这一点在 `glm-5` 上可用——
思考开着、`reasoning_content` 不进历史，两轮工具闭环仍然成功。但同一批冒烟也证明：
**思考模型的输出预算必须容得下推理 token**，预算过小时 `content` 会被挤成空。
依据：[DeepSeek 思考模式](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/)、
[Kimi 参数](https://platform.kimi.com/docs/guide/kimi-k2-6-quickstart)、
[GLM 思考模式](https://docs.bigmodel.cn/cn/guide/capabilities/thinking-mode)。
Kimi 温度为模式固定值，GLM-5 系温度取值范围不含本项目请求默认的 `0.0`，
两者都配 `supports_temperature: false`，省略该参数交给服务端默认值。

`extra_body` 不能覆盖 `model`、`messages`、`max_tokens`、`temperature`、`tools`、
`tool_choice`、`response_format`、`stream`。这些字段由请求与能力标志管理，加载时会拒绝冲突。

## 检查与连通性验证

```bash
uv sync --extra dev --locked
uv run codepilot config check
uv run codepilot llm smoke --alias glm-4.7-zai                       # dry-run，零请求
uv run codepilot llm smoke --alias glm-4.7-zai --live --max-cost 0.05 --budget-currency USD
uv run python scripts/llm_smoke.py deepseek                          # 逐 provider 文本连通性
```

`codepilot llm smoke` 是三阶段冒烟：普通对话、结构化输出（解析成 Pydantic 对象）、
函数调用（真实 `ToolRegistry` 闭环，模型只有真的执行了工具才拿得到答案里的暗号）。
**默认 dry-run，连 HTTP 客户端都不构造**；真实调用必须同时给 `--live` 和 `--max-cost`，
缺任何一条都报成 NOT RUN 而不是"试试看"。费用按 YAML 单价计算并标币种，价格未知或
请求结果未知时报 UNKNOWN，不写成已确认 0。`--json` 输出机器可读结果，其中不含密钥。
空回复与截断回复都判失败。

`scripts/llm_smoke.py` 仍是逐 provider 的文本连通性检查，会先确认费用；`--yes` 跳过交互。

普通对话成功**不能**推断结构化与工具调用也通过——这句话现在有了对应的可执行检查，
不再是只能靠人记得。

未配置 Kimi、GLM 或 OpenAI 密钥时，完整性检查仍会列出这些服务；`--strict` 要求所有
声明的服务与角色配置完整。**币种**也在检查范围内：角色所用模型的单价币种与
`budget_currency` 不一致且 `exchange_rates` 未声明该币种时，检查判为不完整，
live 冒烟与 `bench --allow-live-llm` 都会拒绝开跑（缺汇率时按 1.0 换算会把 USD 费用
低估约七倍，且 `cost_known` 仍为 True，这种数不能放行）。

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
## 2026-10-17 GLM 接入验证（追加，不修改上面的历史记录）

真实冒烟的逐条结果、请求次数与费用只写在
[notes/glm-integration.md](notes/glm-integration.md) §9 一处——两份文档各抄一份数字，
迟早会变成"哪份是真的"这个问题。摘要：5 次 `codepilot llm smoke` 命令、合计 17 个
HTTP 请求、约 $0.0066 USD + ¥0.0040 CNY，`glm-4.7`（两个平台）、`glm-5`、`glm-5.3`
的对话 / 结构化 / 函数调用三条路径全部通过；另有一次故意把输出上限压到 64 的请求
如实报成 FAIL，用来记录"思考模型必须先容得下推理 token"这条约束。

离线侧新增 4 个测试文件与 3 项配置层用例，合计 **141 项**（全量套件从 2665 passed 增至
2806 passed，差值与之一一对应）：配置与币种闸门 / MockTransport 全路径 /
三阶段冒烟引擎 / CLI 装配。最终门禁 2806 passed、1 skipped（原有 embedding 用例）、
44 deselected（docker 与 llm 标记）、覆盖率 92.72%，退出码 0。
E-10（39 题真实跑批）、P-24、G-37 仍为 **NOT RUN**，
这两条结论不能由上面的任何一项互相替代。

密钥只写入被忽略的 `.env`（`git check-ignore .env` 返回 0，`git ls-files` 不含 `.env`）；
提交物、冒烟报告与异常文本中均无密钥值——本轮还修掉了一条真实存在的泄漏路径：
服务商 401 错误体会把密钥回显在 message 字符串里，而按键名脱敏的 `redact()` 拦不住它。

## 2026-10-17 逐请求可观测性与录制接线（追加，不修改上面的历史记录）

第一次真实 GLM 单任务实验暴露出"账目问不出答案"：`events.jsonl` 里 16 条事件的
`usage` 全是 `null`，`Usage.by_model` 只有一个聚合项，`llm_calls.jsonl` 根本不存在。
三条原因都是接线，不是能力：

1. `LLMRegistry` 从来没拿到 `EventSink`。真实 provider 在**派发前**就装配好了，那时
   还没有 `run_id` 与 artifact 目录。现在由 `LLMRegistry.bind_run(...)` /
   `ProviderBundle.bind_run(...)` 在 `InferenceAssembly.run()` 里晚绑定。
2. `settings.record_llm` 只被 CLI 打印过一次，从没接到 `LLMRegistry` 的真实路径。
   现在 `ProviderBundle.live(..., record_llm=True)` → `LLMMode.RECORD`，落点是每个 run
   自己的 `llm_calls.jsonl`。**未绑定落点时 `RecordingLLM` 拒绝发请求**，不产生
   "花了钱但没录下来"的调用。
3. 逐请求元数据此前完全没有这一层。新增 `codepilot/llm/calls.py`：默认就写的
   `llm_requests.jsonl`（每个 run 一份），每条含 `call_id` / `run_id` / `task_id` /
   `seq` / `role` / `node` / `iteration` / `purpose` / `provider` / `model` /
   `attempt` / `correction` / `status` / `finish_reason` / token 四项 / `cost` /
   `cost_known` / `currency` / `error_type` / `http_status` / `tool_calls` / `duration_ms`。

口径上有四条不可含糊：

- **模型别名与角色是两个维度**。`Usage` 同时维护 `by_model` 与 `by_role`，
  `role_totals_reconcile()` 自检两者与总账一致；一个 run 里 planner 与 coder 常常是
  同一个模型，只有 `by_model` 就答不出"贵在哪个环节"。
- **语义纠正重试与服务商 HTTP 重试分开计**。前者写进 `LLMRequest.attempt` /
  `correction` 与 `RepairState.plan_corrections`，后者只存在于 provider 的
  `attempts` / `unacknowledged`。
- **未知费用不记为 0**。`cost_known=False` 的请求单独计数
  (`Usage.cost_unknown_calls`)，`cost_is_lower_bound` 为真时总费用只能报成下界。
- **不同币种不相加**。往一个 `Usage` 里写另一种币种的费用直接抛错。

默认**不**保存：API key、Authorization 头、原始 prompt、未脱敏工具参数、
`reasoning_content`、隐藏测试内容、gold patch。逐请求日志只有元数据；完整请求体只在
显式 `record_llm` 时写，并照旧过 `redact()`。恢复进程会按已有行数续接 `seq`，
`Deps.restore_usage` 改为逐字段覆盖（含 `by_role` 与未知费用计数），避免恢复后
分角色账目与总账脱节。

细节与验证见 [notes/glm-live-hardening.md](notes/glm-live-hardening.md) §4。
