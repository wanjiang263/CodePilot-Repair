# Phase29 HTTP 发布矩阵（T11，2026-10-31）

验证对象：`codepilot.llm.http_lifecycle.send_request` 流式路径 + 两个 adapter
（`openai_compatible` / `anthropic_messages`）。全部请求走 `127.0.0.1` 随机端口
（bind 0），密钥为显式假值 `test-key-not-real`，**真实模型请求 0、外部网络 0、费用 0**。

三态定义（延续 Phase28 口径）：
- **verified**：本轮本地回环测试实测通过（含具体断言）；
- **unverified**：本地无法等价验证，需要真实服务商/Live Gate；
- **unsupported**：当前架构或约束下明确不支持，行为已声明。

## 1. 三态矩阵

| # | 行 | 状态 | 证据 |
|---|---|---|---|
| 1 | 完整响应 + usage 解析（openai/anthropic 双协议；native ledger 结算） | verified | `test_phase28_http_matrix.py` row1（3 例） |
| 2 | 延迟响应头超总 deadline | verified | row2 |
| 3 | 持续慢体触发总 deadline | verified | row3 |
| 4 | 分阶段累计超总 deadline（单段不超时） | verified | row4 |
| 5 | 派发前取消（cancel 路径 + admit 拒绝路径，无 unknown） | verified | row5（2 例） |
| 6 | 派发后取消 → uncertain，无遗留线程 | verified | row6 |
| 7 | 单请求取消不影响其他请求 | verified | row7 |
| 8 | 无隐藏重试：deadline 耗尽后线上恰好 1 次请求 | verified | row8 |
| 9 | 反复超时后无线程遗留 | verified | row9 |
| 10 | gzip 正常解码 + gzip 慢流超时 | verified | row10（2 例） |
| 11 | MockTransport 同步注入（含取消/超时语义） | verified | row11（3 例） |
| 12 | 自定义同步 transport 被 TypeError 拒绝 | verified | row12（2 例） |
| 13 | 代理 env 路由 / NO_PROXY 放行 / trust_env=False 忽略 | verified | row13（3 例） |
| 14 | 连接复用观测（对端端口 + 逐请求耗时） | verified | row14 |
| 15 | Bedrock 执行路径不经过 http_lifecycle（结构 + 运行时断言） | verified | row15 |
| 16 | HTTP 500 + JSON 错误体 → ProviderError（确定性，uncertain=False，线上 1 次） | verified | `test_phase29_http_matrix.py` row16（**本轮新增**） |
| 17 | 200 + 非法 JSON 体 → ProviderError（网关截断归因） | verified | row17（**本轮新增**） |
| 18 | HTTP 429 + Retry-After → RateLimitError；重试可观测（attempts == 线上请求 == 3） | verified | row18（**本轮新增**） |
| 19 | 连接拒绝（回环死端口）→ 传输失败归类（保守 uncertain=True），deadline 截断 | verified | row19（**本轮新增**） |

用例计数：Phase28 矩阵 39 例 + Phase29 错误路径扩展 7 例 = **46 例**（同一命令内运行，不重复计数）。

## 2. 执行记录（三连跑）

| 轮次 | 命令 | 退出码 | 结果 | 日志 |
|---|---|---|---|---|
| run1 | `.venv/Scripts/python.exe -m pytest tests/unit/llm/test_phase28_http_matrix.py tests/unit/llm/test_phase29_http_matrix.py -q` | 0 | 46 passed in 49.45s | `.codex-pytest-tmp/phase29/http-matrix-run1.log` |
| run2 | 同上 | 0 | 46 passed in 54.38s | `.codex-pytest-tmp/phase29/http-matrix-run2.log` |
| run3 | 同上 | 0 | 46 passed in 66.94s | `.codex-pytest-tmp/phase29/http-matrix-run3.log` |

三连跑全部全绿；退出码 0/0/0。

## 3. 未验证项（unverified —— Live Gate 未获授权，未执行）

以下能力本地回环**无法等价验证**，Phase28 已声明边界，本轮维持边界并逐项标注：

1. **真实 provider 端到端**：真实 API 网关、真实模型计费与 usage 口径 —— 未验证（无预算与授权，Live Gate 未执行；每个 provider 最多 1 次最小请求的执行条件见任务书 §7.7）。
2. **Bedrock deadline / 取消**：真实 AWS 端点上的总期限与取消传播 —— 未验证（row15 仅证明执行路径不经过 http_lifecycle，即 Bedrock 走自己的 SDK 路径）。
3. **TLS / 代理 CONNECT**：HTTPS 经 CONNECT 隧道的真实握手 —— 未验证（row13 用 HTTP 回环假代理，非 TLS）。
4. **池化取消**：连接池在高并发取消下的真实套接字回收 —— 未验证（回环观测不等价于真实池化行为）。

## 4. unsupported（明确不支持并已声明）

- 自定义同步 transport（非 MockTransport/HTTPTransport）：TypeError 拒绝（row12）。
- 隐藏重试：总期限耗尽后不允许第二次线上请求（row8）；重试只发生在策略显式允许且可观测的场景（row18）。

## 5. 结论

本地可验证面（19 行 / 46 例）三轮全绿。发布边界与 Phase28 一致并新增错误路径行 16–19；
真实 provider、Bedrock、TLS/CONNECT、池化取消四项维持 **unverified**，不因本地
回环结果改写为"已验证"。真实请求 0，费用 0。
