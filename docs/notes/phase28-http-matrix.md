# Phase28 HTTP 兼容矩阵（三态：verified / unverified / unsupported）

日期：2026-10-30。基线 HEAD `9ccc703`。零真实模型请求、零外部网络：全部 HTTP 走
`127.0.0.1` 回环临时端口（bind 0），密钥全为假值，代理环境仅用 monkeypatch 设置。
证据文件：`tests/unit/llm/test_phase28_http_matrix.py`（39 个测试，openai_compatible 与
native anthropic_messages 两个 adapter 参数化覆盖）。

## 验收命令与结果（真实运行）

| 命令 | 退出码 | 结果 |
|---|---|---|
| `.venv/Scripts/python.exe -m pytest tests/unit/llm/test_phase28_http_matrix.py -q` | 0 | 39 passed（第 1 次，42.01s） |
| 同上（第 2 次） | 0 | 39 passed（43.27s） |
| 同上（第 3 次） | 0 | 39 passed（41.33s） |
| `.venv/Scripts/python.exe -m pytest tests/unit/llm/test_http_total_deadline.py -q` | 0 | 22 passed（31.17s，Phase27 回环组无回归） |
| `.venv/Scripts/python.exe -m ruff check tests/unit/llm/test_phase28_http_matrix.py` | 0 | All checks passed |
| `.venv/Scripts/python.exe -m ruff format --check tests/unit/llm/test_phase28_http_matrix.py` | 0 | 1 file already formatted |

连跑 3 次全部通过：无端口/时序偶发。运行日志：`.codex-pytest-tmp/phase28/matrix-run2.log`、
`matrix-run3.log`、`phase27-regression.log`（第 1 次矩阵运行在写日志前已完成，结果同上；全套无
任何失败，故无首次失败日志需要保留）。

## verified（回环验证，每行给测试名 + 关键断言）

适用范围声明：以下"verified"仅指**本回环实现语义**（httpx 0.28.1 + `http_lifecycle.send_request`
的流式路径），同时覆盖 `openai_compatible` 与 `anthropic_messages` 两个 adapter（`[openai]` /
`[anthropic]` 参数化）。

| 能力 | 状态 | 证据（测试名） | 关键断言 |
|---|---|---|---|
| 正常完整响应 + usage 解析（openai 字段名） | verified | `test_row1_complete_response_parses_usage[openai]` | content=="ok"；input_tokens==10、output_tokens==5；cost>0 且 cost_known；attempts==1；unacknowledged==0 |
| 正常完整响应 + usage 解析（native 字段名） | verified | `test_row1_complete_response_parses_usage[anthropic]` | 同上，finish_reason=="end_turn"（native 语义） |
| native usage 结算（dispatch ledger settled） | verified | `test_row1_native_usage_settles_in_dispatch_ledger` | ledger 记录 status=="settled"、input_tokens==10、output_tokens==5、cost_cny>0、response_id=="phase28-fake-id"、frozen is False |
| 延迟响应头（接近总 deadline 才回头） | verified | `test_row2_delayed_headers_exceed_total_deadline`（双协议） | 服务端 2.6s 才发头；ProviderError.uncertain is True；耗时 ∈ [1.8, 3.2]s；unacknowledged==1；无线程遗留 |
| 持续慢体（deadline 触发 ReadTimeout） | verified | `test_row3_continuous_slow_body_hits_total_deadline`（双协议） | 分块永不单次超时仍被总 deadline 截断；服务端 observed disconnected/finished；server.requests==1 |
| 分阶段累计超总时限 | verified | `test_row4_staged_phases_cumulatively_exceed_deadline`（双协议） | 头 0.8s + 4 个体分块各 0.5s，单段均不超时；累计仍被截断，耗时 ∈ [1.8, 3.2]s |
| 派发前取消（cancel） | verified | `test_row5_cancel_before_dispatch_sends_nothing`（双协议） | ResourceCancelledError；server.requests==0；attempts==0；unacknowledged==0（无 unknown） |
| 派发前 admission 拒绝（admit 回调） | verified | `test_row5_admit_rejection_sends_nothing`（双协议） | 同上：admit 抛 ResourceCancelledError，零线上请求、零 unknown |
| 派发后取消（RequestCancelled） | verified | `test_row6_cancel_after_dispatch_is_uncertain_without_leak`（双协议） | 取消早于 deadline 返回（<1.5s）；ProviderError.uncertain（RequestCancelled 归入 TransportError）；unacknowledged==1；无线程遗留 |
| 一个请求取消不影响另一个/后续 | verified | `test_row7_cancelling_one_request_does_not_affect_others`（双协议） | 慢请求取消的同时快请求 content=="ok"；worker 线程结束；取消后再发请求仍成功；server.requests==3 |
| 无隐藏重试（恰好 1 次线上请求） | verified | `test_row8_no_hidden_retry_single_wire_request`（双协议） | 默认 4 次尝试策略下 server.requests==1、attempts==1；cause 为 DispatchStoppedError（不掩盖"已派发"事实） |
| 超时后无线程遗留 | verified | `test_row9_no_thread_leak_after_repeated_timeouts` + 每个超时/取消测试内的 `assert_no_http_worker_threads()` | 两次连续超时后 `threading.enumerate()` 无 `codepilot-http`；后续请求仍成功（共享 client 未被关掉） |
| gzip 正常解码语义 | verified | `test_row10_gzip_decodes_once_with_decoded_length`（双协议） | `response.content == BODY`（恰好解码一次，非 gzip 字节）；无 `content-encoding`/`transfer-encoding` 头；`content-length == len(解码后明文)` |
| gzip 慢流超时 | verified | `test_row10_slow_gzip_stream_hits_total_deadline`（双协议） | 逐字节 gzip 滴流仍被总 deadline 截断；server.requests==1；无线程遗留 |
| MockTransport 同步注入 | verified | `test_row11_mock_transport_synchronous_injection`（双协议） | handler 内同步返回；1 次调用、usage 正常解析、attempts==1、无 unknown、无线程 |
| MockTransport 注入路径的取消/超时语义 | verified | `test_row11_mock_transport_cancel_after_dispatch_raises_requestcancelled`、`test_row11_mock_transport_past_deadline_raises_readtimeout` | 注入路径上 post-dispatch 取消抛 `RequestCancelled`（TransportError 子类）；deadline 已过抛 `httpx.ReadTimeout`；均同步、无线程 |
| 自定义同步 transport 被拒绝 | verified | `test_row12_custom_sync_transport_rejected_with_typeerror`、`test_row12_custom_sync_transport_rejected_through_provider`（双协议） | 非 MockTransport/HTTPTransport 抛 `TypeError("Cancellable HTTP requires HTTPTransport or offline MockTransport")`；经 provider 调用时 TypeError 原样穿透（attempts==1，不可重试） |
| 代理环境：HTTP_PROXY/HTTPS_PROXY 生效 | verified | `test_row13a_proxy_env_routes_request_through_loopback_proxy`（双协议） | 回环假代理收到恰好 1 次请求（proxy.requests==1），origin.requests==0，响应经代理正常解析 |
| NO_PROXY 放行回环直连 | verified | `test_row13b_no_proxy_bypasses_proxy_for_loopback`（双协议） | NO_PROXY=127.0.0.1 时 proxy.requests==0、origin.requests==1 |
| trust_env=False 忽略环境代理 | verified | `test_row13c_trust_env_false_ignores_proxy_env`（双协议） | 设置了 HTTP_PROXY 仍直连 origin；proxy.requests==0 |
| 连接复用观测（无 TLS 回环） | verified | `test_row14_connection_reuse_observation` | HTTP/1.1 keep-alive 服务端记录 5 次请求的**对端端口互不相同**（5 条独立 TCP 连接）；逐请求耗时 15.0/16.0/0.0/16.0/15.0 ms（单次 <2s 断言） |

**连接复用结论：保留请求私有 client。** 依据：`send_request` 对 HTTPTransport 每次请求新建私有
`httpx.AsyncClient` 并在退出时关闭，回环观测确认不存在跨请求连接复用（5/5 条独立连接）。复用
只能在池化 client 上发生，而池化与"请求级取消后关闭传输再返回"的取消语义（Phase27 冻结）冲突；
且回环实测单次请求总耗时中位数 ~15ms，其中新建连接的增量在毫秒级。除非未来有真实服务商数据
显示连接建立是显著瓶颈，否则不改此语义。

**Bedrock（协议 bedrock_converse）：执行路径边界 = verified；deadline/取消语义 = unverified。**

| 能力 | 状态 | 证据 | 关键断言 |
|---|---|---|---|
| Bedrock 不经过 http_lifecycle | verified | `test_row15_bedrock_execution_path_bypasses_http_lifecycle` | 结构断言：模块源码无 `http_lifecycle`/`send_request`/`httpx`/`asyncio`；运行时断言：把 `http_lifecycle` 与两个 adapter 的 `send_request` 全部下毒后，Bedrock 仍以 fake runtime client 完整跑通（converse 恰好 1 次调用） |
| Bedrock 的总 deadline / 派发后取消 / usage 冻结 | **unverified** | 无测试 | `BedrockConverseProvider.complete` 不建 dispatch scope、不接 cancel 回调，本轮不联网验证；不得声明"已支持" |

## unverified 清单（本轮不声明支持，禁止写"已支持"）

1. **Bedrock 的 deadline / 派发后取消 / unknown 记账**：执行路径已证明绕过 `http_lifecycle`
   （见上表 verified 行），因此 Phase27 冻结的取消与总 deadline 保证**不适用于** Bedrock 路径；
   其在真实 AWS SDK 行为下的表现未验证。依赖此语义的调用方不应假设 Bedrock 可取消。
2. **DNS / OS 级停顿**（解析慢、socket 建连前挂起、进程暂停恢复）：回环测试不产生 DNS 流量，
   `connect` 阶段的 OS 停顿未单独验证；总 deadline 覆盖 connect 之后的阶段已验证，connect 之前
   的极端停顿未验证。
3. **长连接 / keep-alive 复用下的取消语义**：本矩阵显式观测到"不复用"（结论=保留私有 client），
   因此"复用连接被取消时对共享连接的影响"这一场景在本实现下不存在；若未来改为池化方案，该
   场景必须重新验证。row14 的 HTTP/1.1 服务端只用于观测，不构成对复用语义的验证。
4. **真实服务商（GLM 之外）**：本轮零真实请求。GLM 真实链路已在 Phase27 验证（见
   `docs/notes/glm-live-hardening.md` 与 Phase27 验收记录，此处引用不重复）；OpenAI / Anthropic
   真实端点行为未验证。
5. **HTTPS/TLS 回环**：全部回环均为 http://。TLS 握手时长、代理 CONNECT 隧道、证书链错误路径
   未验证（代理行验证的是 http:// 绝对 URI 转发语义，不是 CONNECT）。

## unsupported 清单（设计上明确不支持，有测试锁定）

1. **自定义同步阻塞 transport**（非 `httpx.MockTransport`、非 `httpx.HTTPTransport`）：
   `send_request` 直接抛 `TypeError`（`test_row12_*` 锁定）。任意阻塞 transport 无法承诺取消，
   属于显式不支持，而非"未验证"。
2. **注入同步 client 时自定义 TLS / 代理设置不复制到实际请求**：`send_request` 对 HTTPTransport
   路径新建的私有 `AsyncClient` 只带 `timeout` 与 `trust_env` 两个参数（`http_lifecycle.py`
   `exchange()`），模板 client 上的 `verify=`、`cert=`、`proxy=`、`http2=`、`limits=`、自定义
   `event hooks` 等**不会被复制**。`trust_env` 会传递（row13c 验证）；其余设置对线上请求无效果。
   依赖这些设置的调用点视为 unsupported。
3. **对非 HTTPTransport 的"取消感知"**：除 MockTransport（同步注入，仅提供 post-dispatch
   取消/超时两个检查点）外，无任何同步 transport 能参与取消；这不是可配置项。

## 边界与方法说明

- 总时限来源：`DispatchScope`（`deadline_scope`，SCOPE_SECONDS=2.8）经 `dispatch_timeout` 取整后
  实际生效 **2.0s**（与 Phase27 相同模式）；所有耗时断言窗口 [1.8, 3.2]s 与该值一致。
- 假服务端：`ThreadingHTTPServer` bind `127.0.0.1:0`，行为用 `by_purpose` 按 `X-CodePilot-Purpose`
  头分发；HTTP/1.0 + Content-Length 保证对端断开可观测（disconnected/finished）。
- 代理验证方法：回环"假代理"收到 http:// 绝对 URI 的 POST 即证明请求被代理（origin 计数为 0）；
  环境变量全部经 `monkeypatch` 设置/清除（`_PROXY_ENV_KEYS` 8 个键），进程级配置未被触碰。
- `httpx 0.28.1` 的 NO_PROXY/HTTP_PROXY 挂载行为在写测试前用一次性探针确认过（`URLPattern`
  mounts），与本机 Windows 会话残留的 NO_PROXY 无关（测试先清空全部代理键再设置）。
