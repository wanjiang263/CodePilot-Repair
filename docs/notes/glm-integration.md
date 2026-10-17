# GLM 官方 API 接入（Z.AI / BigModel）

日期：2026-10-17。分支：`codex/glm-api-integration`。本轮目标是把智谱 GLM 接进**现有**
LLM Provider 体系，不是重新做一套调用脚本。

结论先说：GLM 已通过现有 `OpenAICompatibleProvider` 接入，普通对话、结构化输出、
函数调用三条路径都在 MockTransport 下通过，并在**真实 API** 上各通过一次极小冒烟。
真实 Issue 修复能力（E-10 / P-24 / G-37）仍未验收，见文末。

---

## 1. 两个平台，两个标准端点

| 平台 | provider 名 | base_url | 请求路径 | 公开计价币种 |
|---|---|---|---|---|
| Z.AI 国际站 | `zai` | `https://api.z.ai/api/paas/v4` | `<base>/chat/completions` | USD |
| 智谱 BigModel 中国站 | `bigmodel` | `https://open.bigmodel.cn/api/paas/v4` | `<base>/chat/completions` | CNY |

两处都是标准 Chat Completions，鉴权一律 `Authorization: Bearer <API Key>`。
`httpx` 会把 `base_url` 的路径段与 `/chat/completions` 正确拼接，实测不产生双斜杠，
尾斜杠也不会破坏路径（`tests/unit/llm/test_glm_provider.py` 有断言锁住这件事）。

**明确不接的端点**（`glm.py` 会直接拒绝，不给"试一下"的机会）：

- `.../api/coding/paas/v4`：Coding Plan 套餐端点。它按**积分**而不是 token 计费，
  并且官方公告写明调用 `glm-4.7` / `glm-5.1` 会被自动改写到别的模型上。接到这个
  地址上，"模型 ID × token 单价"这条费用口径整段失效，而报表上看不出任何异常。
- `.../api/anthropic`（Anthropic 形态）与 `.../api/v1`（Responses 形态）：请求体字段
  不同，共用一个客户端只会得到一批难以归因的 400。

标准 API 密钥与套餐密钥官方声明互不通用。本轮实测：**同一把标准 API 密钥在
`zai` 与 `bigmodel` 两个标准端点都通过鉴权**（各 1 次极小请求）——这是"这把密钥"的
观测结果，不等于所有密钥都跨站通用，也不涉及套餐密钥。

## 2. 密钥与环境变量

密钥值只进 `.env`（已被 `.gitignore` 忽略且未被 git 跟踪），配置里只写变量名。

```ini
GLM_API_KEY=<密钥>
GLM_PLATFORM=zai          # zai | bigmodel，填错直接报错，不退回默认值
GLM_BASE_URL=             # 可选覆盖
GLM_MODEL=                # 可选覆盖：换官方模型 ID，不改 YAML
GLM_TIMEOUT_SECONDS=      # 可选覆盖 provider 超时
GLM_MAX_RETRIES=          # 可选覆盖 provider 重试次数
```

`bigmodel` provider 的读取顺序是 `GLM_API_KEY` → `ZHIPU_API_KEY`（历史变量名作为
fallback 保留）。fallback 写在 `ProviderSpec.api_key_env_fallback` 里，读密钥仍然只有
`ProviderSpec.api_key()` 一条路径——没有第二套 `.env` 解析逻辑。

覆盖只在**至少出现一个 `GLM_*` 变量**时才生效：DeepSeek/Kimi 的运行不会因为这段代码
存在而被多塞一个 provider，那会让 `provider_config_hash` 与历史批次对不上。

密钥不出现在 YAML、日志、Checkpoint、Artifact、异常消息和冒烟报告里。这里有一条
本轮真实修掉的漏洞：**服务商的 401 错误体会把密钥原样回显在 `message` 字符串里**，
而 `observability.redaction.redact()` 按**键名**脱敏，对"密钥作为某个值的一部分"无能
为力。修复放在持有密钥的 provider 层（`OpenAICompatibleProvider._scrub`），三家服务商
一并受益；对应测试是 `test_key_echoed_inside_error_message_is_scrubbed_at_the_source`。

## 3. 已配置的 GLM 别名

模型 ID 与单价一律只在 `configs/llm.yaml` 里，代码里一个字都不写。

| 别名 | provider | 官方模型 ID | 上下文 | 单价币种 | 输入/输出/缓存命中（每百万 token） |
|---|---|---|---|---|---|
| `glm-main` | bigmodel | `glm-4.7` | 200K | CNY | 4 / 16 / 0.8 |
| `glm-5-main` | bigmodel | `glm-5` | 200K | CNY | 6 / 22 / 1.5 |
| `glm-5.1-main` | bigmodel | `glm-5.1` | 200K | CNY | 8 / 28 / 2 |
| `glm-5.3-main` | bigmodel | `glm-5.3` | 1M | CNY | 8 / 28 / 2 |
| `glm-4.7-zai` | zai | `glm-4.7` | 200K | USD | 0.6 / 2.2 / 0.11 |
| `glm-5-zai` | zai | `glm-5` | 200K | USD | 1.0 / 3.2 / 0.2 |
| `glm-5.1-zai` | zai | `glm-5.1` | 200K | USD | 1.4 / 4.4 / 0.26 |
| `glm-5.3-zai` | zai | `glm-5.3` | 1M | USD | 1.4 / 4.4 / 0.26 |
| `glm-5.3-main` | bigmodel | `glm-5.3` | 1M | CNY | 8 / 28 / 2 |
| `glm-5.3-zai` | zai | `glm-5.3` | 1M | USD | 1.4 / 4.4 / 0.26 |

来源与口径（两页均按 2026-10-17 逐行取原文）：BigModel 定价页
（`docs.bigmodel.cn/cn/guide/start/pricing`）按**输入长度分档**，`glm-4.7` 还额外按
输出长度分档；Z.AI 定价页（`docs.z.ai/guides/overview/pricing`）**不分档**。
YAML 里一律取公开最高档，用于预算估算——保守、偏高，不等于账单。已核实的完整分档：

| 模型 | BigModel（输入 / 输出 / 缓存命中，CNY） | Z.AI（输入 / 缓存 / 输出，USD） |
|---|---|---|
| `glm-4.7` | 输入<32K 且输出<0.2K：2 / 8 / 0.4 · 输入<32K 且输出≥0.2K：3 / 14 / 0.6 · 输入≥32K：4 / 16 / 0.8 | 0.6 / 0.11 / 2.2 |
| `glm-5` | 输入<32K：4 / 18 / 1 · 输入≥32K：6 / 22 / 1.5 | 1.0 / 0.2 / 3.2 |
| `glm-5.1` | 输入<32K：6 / 24 / 1.3 · 输入≥32K：8 / 28 / 2 | 1.4 / 0.26 / 4.4 |
| `glm-5.2` · `glm-5.3` | 8 / 28 / 2（不分档，1M 上下文） | 1.4 / 0.26 / 4.4 |
| `glm-4.6` | **BigModel 定价页没有它**（只有 4.6V 视觉模型）→ 不建 CNY 别名 | 0.6 / 0.11 / 2.2 |

三件必须写下来的事：

1. **缓存命中价是替换价，不是额外加价。** BigModel 把费用写成
   `未命中缓存的输入费用 + 缓存命中费用 + 输出费用 + 缓存存储费用`，四项里的前三项覆盖
   **互斥**的 token 集合；两家缓存文档都写"缓存命中 Token：按优惠价格计费"。
   `cost.py` 的做法（从全价输入里扣掉命中部分、按 `cached_input_per_mtok` 计）与此一致。
   口头说的"通常为标准价格的 50%"与表格实际的 18–25% 不一致——**取表格，不取口头**。
2. **两家数字并不一致，且没有任何一页给出换算关系。** 同一个 `glm-5` 是 ¥4–6 还是
   $1.00，两页各说各话。所以**不许从一家的价格推导另一家的价格**，也不许把两种币种
   混进同一张预算表：要么显式声明汇率，要么用同币种预算，否则 `config check` 与
   live 冒烟都会拒绝放行。
3. **有两项费用无法建模**：独立的"缓存存储费用 / Cached Input Storage"（两家都标
   `限时免费`，标准价未公布），以及支持 Batch API 的"标准调用价 5 折"。因此按配置算出的
   费用**系统性不等于账单**；`glm-4.7` 的输出长度分档也无法由单次请求预知，只能按最高档估。

`glm-5.2` 与 `glm-5.3` 在两家的模型页与定价页都在（1M 上下文，数字相同）。本轮为
`glm-5.3` 建了别名并用**真实 API 验证可调用**（三阶段全通过，见 §9）；`glm-5.2` 没有建——
它与 5.3 数字相同、本轮未实测，多一个没验过的别名只是多一个可能写错的前提。

## 4. Thinking / reasoning_content

- 能力由 `ModelSpec.supports_thinking` 表达，不是 `if model == ...`。
- `glm-4.7` 别名带 `extra_body: {thinking: {type: disabled}}`；**真实调用已验证该参数
  被两家标准端点接受**（此前只是文档断言，且两份资料互相矛盾）。
- `glm-5` / `glm-5.1` 别名 `supports_thinking: false`：思考关不掉，于是**不下发**该参数。
  `supports_thinking: false` 同时会把 `thinking` / `reasoning_effort` 从请求体里摘掉；
  若有人在 `extra_body` 里矛盾地又写上 `thinking`，加载期直接报错。
- `reasoning_content` 只记录、不回流：不进入下一轮 messages，也不进入 Run Report 正文。
- 温度：GLM-5 系取值范围受限（本项目请求默认 `temperature=0.0`，`glm-5.1` 会越界），
  因此这些别名 `supports_temperature: false`，省略该参数交给服务端默认值——
  与 Kimi 用的是同一个既有机制。

**真实 API 观察到的一条硬约束**：用 `--max-output-tokens 64` 冒烟 `glm-5` 时，
模型把 64 个输出 token 全部花在思考上，`content` 为空、`finish_reason=length`，
`reasoning_content` 有 111 字符。把上限提到 1500 后三阶段全部通过
（思考实际消耗 212–979 个输出 token）。

引擎当时把它判成 FAIL 并写明了原因，这是对的——空答案就是空答案。但运营含义很重要：
**给 GLM-5 系的输出预算必须容得下推理 token**，否则会稳定表现为"模型没话说"。
Agent 侧目前每次请求 `max_output_tokens=4096`，对 `glm-5` 系是否够用**未经验证**；
真要跑自主修复，这是第一个要盯的数（未完成项见 §12）。

## 5. 结构化输出

GLM 支持 `response_format: {"type": "json_object"}`；**strict JSON Schema
（`{"type": "json_schema"}`）在两家官方文档里都没有记载**，因此本项目不依赖它：
Schema 同时写进 prompt，拿到 JSON 后由本地 Pydantic 校验，失败时把"上次输出原文 +
具体校验错误"发回去纠正重试（沿用 `complete_structured` 的三次上限）。

离线测试用四份**真实** Agent Schema 验证解析：`IssueAnalysis`、`RepairPlan`、
`Diagnosis`、`ReviewResult`。同时锁住这些行为：缺字段/枚举错/类型错必须明确失败；
重试耗尽抛 `StructuredOutputError`；三次尝试的 token 全部计入费用；截断的 JSON
不被当作合法结果伪造。

真实冒烟用的是一个紧凑探针 Schema（`SmokeVerdict`：必填 + 枚举 + 嵌套数组），
在 `glm-4.7` 与 `glm-5` 上各一次通过。**冒烟证明的是"结构化输出这条通路可用"，
不是"模型能产出正确的修复计划"**——后者需要真实 Issue，属于 E-10。

## 6. Function Calling

发出去的工具表来自真实 `ToolRegistry`（`registry.specs(role)`），权限隔离照原样生效：
Planner 只有六个只读工具，Coding Agent 用授权集合，Review 拿不到写权限，未注册工具名
一律拒绝。`arguments` 是 JSON 字符串，先解析、再按工具的 Pydantic `input_model` 校验，
工具结果按 `tool_call_id` 关联回传。模型自行给出的 shell 命令不会被执行；GLM 平台侧的
内置代码执行工具不在本项目的工具表里，因此不存在"绕过 Sandbox"的路径；
30 次工具调用上限由 `CodingAgent` 自己守住（离线测试覆盖了这一条）。

真实冒烟的工具阶段是一个完整闭环：请求 → 模型给出 `read_file` 调用 → CodePilot 校验
并在临时 workspace 上真实执行 → 结果回传 → 模型第二轮答出暗号。暗号 `GLM-SMOKE-7QX4`
只存在于那个临时文件里，**模型不可能凭想象答出来**——这正是它作为判别器的用途。
`glm-4.7` 与 `glm-5` 各验证一次，均 PASS。

## 7. 错误、重试与费用

- 映射沿用既有 Provider Error 体系：429 → `RateLimitError`，5xx/超时/传输失败 →
  `ProviderError`，上下文超限 → `ContextTooLongError`。
- 只重试 408/409/429/500/502/503/504。**401/403/404/400 不重试**（离线测试断言
  `HTTP 次数 == 1`）：重发一次鉴权失败只会多花三倍延迟和配额。
- `max_retries` 现在是 `ProviderSpec` 的 per-provider 字段（GLM 两个 provider 设为 2），
  `LLMRegistry` 据此覆盖全局 `RetryPolicy` 的尝试次数。退避仍由本地策略给出，
  因为**官方限流文档不提供 `Retry-After` 头**——这条已写成测试。
- httpx 客户端没有内置重试，因此不存在"SDK 重试 × 项目重试"叠乘。
- 请求次数与"已发出但结果未知"的次数由 provider 自己计数（`attempts` /
  `unacknowledged`）。超时和连接中断意味着服务端可能已经生成并计费，冒烟报告据此把
  `cost_known` 置为 False 并标 `usage_uncertain=true`。不承诺 exactly-once。
- 币种：Z.AI 是 USD、BigModel 是 CNY。角色所用模型的单价币种与预算币种不一致且
  `exchange_rates` 没声明该币种时，`config check` 判配置不完整，live 冒烟与 `bench
  --allow-live-llm` 都**拒绝开跑**——缺汇率时 `convert()` 按 1.0 处理，会把 USD 费用
  低估约七倍还标成"费用已知"。可以用 `--budget-currency USD` 直接同币种结算。
- 价格缺失 → `cost_known=False`，报表写 UNKNOWN，不写成已确认 0。
- `--allow-live-llm` 继续是 `solve` / `bench` 的唯一真实调用开关，冒烟命令另需 `--live`。

## 8. 怎么用

```bash
# 看配置：缺密钥 / 缺模型 ID / 缺价格 / 币种缺汇率
uv run codepilot config check

# dry-run（默认，零请求）
uv run codepilot llm smoke --alias glm-4.7-zai

# 真实冒烟：必须显式 --live + 费用上限；USD 计价的别名要与预算币种一致
uv run codepilot llm smoke --alias glm-4.7-zai --live --max-cost 0.05 \
    --budget-currency USD --max-output-tokens 1500

# 换平台 / 换模型而不改 YAML
uv run codepilot llm smoke --alias glm-5-main --glm-platform bigmodel --model glm-5.1

# 跑真实 Agent：角色映射仍由 configs/llm.yaml 决定，或用 --llm-model 整批覆盖
uv run codepilot solve  --repo <仓库> --issue <Issue> --allow-live-llm --llm-model glm-main
uv run codepilot bench  --seeds 1 --allow-live-llm --llm-model glm-5-main --max-total-cost 5
```

`bench` 的正常命令仍走 Phase 10 那套完整 M2 图，没有为 GLM 另开简化流程。

## 9. 真实 API 冒烟结果

用户本轮授权的是 GLM Provider 集成，**不是** 39 题真实跑批。下表是**最终代码**上的真实
冒烟：5 次命令、合计 **17 个 HTTP 请求**，全部极小规模，未触发任何重试。

| 命令目标 | 平台/模型 | HTTP | 各阶段 token in/out | 费用 | 结果 |
|---|---|---|---|---|---|
| 三阶段全跑 | zai / `glm-4.7` | 4 | 10/2 · 344/57 · 348/11 | $0.0005752 | 三阶段 **PASS** |
| 三阶段全跑 | zai / `glm-5.3` | 4 | 17/93 · 300/318 · 356/134 | $0.0033402 | 三阶段 **PASS** |
| 三阶段全跑 | bigmodel / `glm-4.7` | 4 | 10/2 · 344/64 · 348/11 | ¥0.00404 | 三阶段 **PASS** |
| 三阶段全跑（输出上限 1500） | zai / `glm-5` | 4 | 17/141 · 353/379 · 356/76 | $0.0024284 | 三阶段 **PASS** |
| 单阶段（输出上限 64） | zai / `glm-5` | 1 | 17/64 | $0.0002218 | **FAIL（有效结论）** |

合计 **$0.0065656 USD + ¥0.00404 CNY**，所有阶段 `cost_known=true`、
`usage_uncertain=false`——没有"已发出但结果未知"的请求，因此这笔数是确定的。
两次 `--max-cost 0.05` 的上限都远未触及。

每一条真实结论对应什么：

- **认证与端点**：`api.z.ai` 与 `open.bigmodel.cn` 两个标准端点都通过，
  且**同一把密钥在两站都能用**（各 4 次真实请求）。这是这把密钥的观测结果，
  不等于所有密钥跨站通用，也与套餐端点无关。
- **模型可用性**：`glm-4.7`、`glm-5`、`glm-5.3` 全部可调用。`glm-5.3` 尤其值得记一笔：
  有一份资料断言它还不存在，真实调用直接推翻了那个说法。`glm-4.5` / `glm-4.6` 本轮未测。
- **结构化输出**：三次 `response_format=json_object` 请求都返回了可被本地 Pydantic
  校验通过的 JSON（探针 Schema 含必填、枚举与嵌套数组）。
- **函数调用**：三次工具闭环都通过——模型给出 `read_file` 调用、CodePilot 用真实
  ToolRegistry 校验并执行、结果按 `tool_call_id` 回传、模型第二轮答出只存在于临时文件里的
  暗号 `GLM-SMOKE-7QX4`。`glm-5` / `glm-5.3` 的**思考模式开着**跑工具也通过了，
  且 `reasoning_content` 没有被回传。
- **`thinking: {type: disabled}` 在 `glm-4.7` 上被两家端点接受**：此前这只是配置里的
  一句断言，两份资料互相矛盾，现在有了实测。

最后那条 FAIL 是**有效结论**，不是故障：`glm-5` 把 64 个输出 token 全部花在思考上
（`reasoning_content` 139 字符），`content` 为空、`finish_reason=length`。引擎如实
报告"未取得完整文本回复"，没有把思考内容当答案。它的运营含义写在 §4：**思考模型的
输出预算必须容得下推理 token**。

这些结论的边界：真实冒烟证明的是**认证、端点、模型可用性、JSON 模式、工具协议**这五件事
——通了。它**不证明**模型能修复真实 Issue，也不构成 L-18 / E-10 / P-24 / G-37 的
效果验收。

## 10. 离线测试与最终门禁

MockTransport 离线用例（零网络、零费用，用**仓库里那份 `configs/llm.yaml`** 而不是另造
配置——配置写错要在这里炸，而不是等一次真实付费调用去发现）：

| 文件 | 用例 | 覆盖 |
|---|---|---|
| `tests/unit/llm/test_glm_config.py` | 39 | 平台画像、`GLM_*` 覆盖、端点拒绝、币种闸门、密钥 fallback、per-provider 重试（按真实 HTTP 次数断言）、装饰器链剥开、仓库 YAML 自检 |
| `tests/unit/llm/test_glm_provider.py` | 56 | 两平台 URL 拼接、请求体形状、四份真实 Agent Schema、纠正重试与费用累计、单/多 ToolCall、完整工具闭环、未注册工具、越界路径、30 次上限、`reasoning_content` 不回流、401/403/404/400 不重试、429/5xx/超时/连接失败、畸形响应体、缓存折扣计价、密钥回显脱敏 |
| `tests/unit/llm/test_glm_smoke.py` | 29 | dry-run 零请求、live 四类前置拒绝、三阶段判定、预算耗尽停派、JSON 报告无密钥、结果未知请求 |
| `tests/unit/test_cli_glm.py` | 14 | `llm smoke` 参数与退出码、生效配置装配、`bench --allow-live-llm` 回归、config hash / model identity 跟随覆盖 |
| `tests/unit/llm/test_config.py` | +3 | YAML 重复键在加载期拒绝 |

最终门禁（对应提交的生产代码）：

```
ruff check .            → All checks passed!
ruff format --check .   → 337 files already formatted
mypy src                → Success: no issues found in 156 source files
pytest -m "not docker and not llm" --cov
                        → 2806 passed, 1 skipped, 44 deselected in 34:06，exit 0
                        → Total coverage: 92.72%（阈值 80% 未下调）
```

那 1 项 skipped 是原有的 embedding 用例（需要 `CODEPILOT_TEST_EMBEDDING_MODEL`），与
GLM 无关；44 deselected 是 docker 与 llm 标记，与基线一致。基线 2665 → 2806 的差值 141
与上表用例数一一对应，**没有一项靠 skip 或 deselect 掩盖兼容问题**。

## 11. 本轮顺手修掉的既有缺陷

1. `bench --allow-live-llm` 之前对 `configs/llm.yaml` 做 `json.loads()`，必然报
   "LLM 配置读取失败"。live 跑批这条路在 Phase 10 从未真跑过（E-10 是 NOT RUN），
   所以没人踩过。现在走 `load_llm_config`，并且**同一份生效配置**同时用于
   `provider_config_hash`、`model_identity` 和真正构造的 provider。
2. `model_identity` 在 live 批次里原本恒等于字符串 `"configs/llm.yaml"`，事后无法判断
   那批分数属于哪个模型。现在由 `role_model_identity(config)` 给出"角色=官方模型 ID"
   的完整映射；GLM 覆盖生效时指纹与身份都会跟着变（有测试锁住）。

顺带：`LLMResponse` 新增 `finish_reason` 透传字段（各家取值集合不同，代码里不按取值
分支）；`UsageTrackingProvider` / `RecordingLLM` 暴露 `inner`，供需要真实请求次数的
场合剥开装饰器链。

## 12. 已知限制与未完成验收

- **E-10（39 题真实跑批）、P-24、G-37：仍为 NOT RUN**，本轮未执行、也未被这些冒烟
  替代。Phase 10 的历史 Benchmark 成功率未改动。
- **单任务真实自主修复实验**：入口已备好（`solve --allow-live-llm --llm-model glm-main`
  走完整 M2 图 + ToolRegistry + PatchValidator + DockerSandbox + Regression + Review），
  但本轮**未自动执行**。真跑之前要先决定 §4 那条输出预算问题，并显式给出整批费用上限。
  不给 Gold Patch 进推理上下文、不用 Gold Patch 生成脚本这些约束由 Phase 10 的装配保证，
  未受本轮改动影响。
- GLM 的**思考模式 + 多轮工具调用**在离线有专测（reasoning 不回流），真实 API 只验证了
  "能跑通"，未验证长思考下的稳定性与费用分布。
- 单价是**文档标价估算**，未与账户账单对账；分档一律取最高档；缓存存储费与 Batch API
  折扣未建模。实际扣费以厂商控制台为准。
- 汇率未写进 `configs/llm.yaml`（`exchange_rates` 只有 `CNY: 1.0`）：跨币种结算要么由
  操作者声明汇率，要么用同币种预算。宁可拒绝运行，也不编一个汇率数字。
- `codepilot bench --require-validated` 只有正向开关（`--no-require-validated` 不存在），
  本轮记录该现象，未改 CLI 表面。
- 未实现：流式（Streaming）。本轮按任务要求先做非流式；现有公共接口不要求流式，
  因此没有另加。
- 官方资料中互相矛盾之处（`glm-5.3` 是否存在、`thinking` 对各模型的支持矩阵、缓存价
  语义、分档边界）一律以**真实调用观测**或**定价表原文**为准，口头描述不采信。
