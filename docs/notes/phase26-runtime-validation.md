# Phase26 运行验证与最终验收（2026-10-29）

状态：**通过**。基线 HEAD `07625a83f7d39db3f88ddde4ebd5005851978afa`，候选 `phase26-candidate-v5`（父版本 v4，pending，无 active 指针）。真实模型请求 0，新增模型费用 0。机器可读结果见 [phase26-runtime-results.json](phase26-runtime-results.json)，静态与归档核对见 [phase26-static-checks.json](phase26-static-checks.json) 和 [phase26-final-archive-integrity.json](phase26-final-archive-integrity.json)。

## 1. 三个风险如何解决

### 1.1 HTTP 请求总 deadline 与可靠取消

分阶段 connect/read/write/pool timeout 只限制单阶段，持续慢速响应体可以无限拖长；本轮加入**按请求单调时钟计算的唯一总 deadline**。

- `src/codepilot/llm/openai_compatible.py`：`_send_before_deadline` 以 stream 发送，每个响应块后检查 `time.monotonic() > deadline`；超限抛 `httpx.ReadTimeout("请求总时限 %ss 已耗尽：持续响应超过 deadline")`。分块不能重置总计时。完整响应才重建 Response；退出上下文只关闭该响应，不关闭任务共享 client。
- `src/codepilot/llm/retry.py`：`call_with_retry` 新增 `before_retry` 回调，在 `on_retry` 之后、sleep 之前执行；抛出则 `raise exc from stop`。`openai_compatible.complete` 传入 `lambda: check_dispatch("model")`，避免超时后的再次派发把已冻结的 unknown 计费覆盖成另一种拒绝。
- 未新增隐藏重试：超时即结束该请求，调用方观察一次模型调用计数。
- 计费语义：派发前取消 → 不写 unknown；已发出且 usage 不可确认 → unknown 并冻结；收到可信完整 usage → settled 正常结算。原有授权、冻结与历史账本未改写。

本地回环验证：`tests/unit/llm/test_http_total_deadline.py` **10 passed，19.20s**，覆盖延迟响应头、持续慢速响应体、分阶段累计超总时长、正常完整响应、派发前取消、派发后超时、单请求取消不影响另一请求、无隐藏重试、超时后无遗留、settled/unknown/cancelled admission 分类。日志：`.codex-pytest-tmp/phase26-runtime/` 下由 A 组日志 `final-A-group.log` 记录执行；服务器仅绑定 127.0.0.1。

### 1.2 线程与事件循环所有权

| 对象 | 所有权与边界 | 依据 |
|---|---|---|
| SQLAlchemy session / RepositoryIndex | 单个任务图内节点串行执行，下一个节点开始前上一节点已结束；不跨任务并发共享 | `tests/graph/test_phase26_ownership.py::test_consecutive_nodes_share_the_index_and_emit_ordered_events` |
| checkpointer | 只在任务级事件循环创建和使用；节点私有循环不复用 | `src/codepilot/graph/builder.py` 中 `asyncio.to_thread(execute_node)` + 每节点 `asyncio.run(invoke_node())`；`open_checkpointer` 为协程 |
| EventSink | `threading.RLock` 包住整个 emit 路径，seq、JSONL、API 镜像与 runtime 投影原子；close/last_seq 同样取锁 | `src/codepilot/observability/events.py`；8 线程 × 25 次 emit 得到 seq 1..200 |
| httpx client | 同步 client 只经线程安全连接池访问，不在节点循环内创建异步 client | ownership 测试两独立任务并行无共享状态 |
| usage / test_runs | 每次调用由所属工作线程更新；任务循环只在节点边界读取 | 取消后 usage.llm_calls 保留 |
| contextvars / deadline / 取消 / 来源 | 在节点边界建立，to_thread 复制 context；`dispatch_scope` 在工作线程内生效 | `test_cancel_during_a_model_wait_stops_at_the_node_boundary` |

取消收尾：`CancelledError` 不丢弃工作线程——`asyncio.shield` 后显式 `await execution_task` 等待在途副作用结束，再向外抛取消。没有用捕获并忽略线程异常代替修复。

关键修复：`src/codepilot/graph/checkpoint.py` 的 `open_checkpointer` 改为 `connection = await aiosqlite.connect(str(target))`。缺少 await 时 `__aexit__` 的 commit 抛 `ValueError("no active connection")`，掩盖 `CheckpointIdentityError`，导致恢复测试误判。

完整调用链验证：`tests/graph/test_phase26_ownership.py` **5 passed**（连续节点、两个独立任务、DB 访问与事件序号、模型等待期取消、取消后 checkpoint 读取与恢复不重放）；`tests/graph/test_resume.py` **16 passed**；批次组通过。取消后 checkpoint 读回 `CANCELLED`，恢复运行未重复已完成 Fake 调用，无遗留线程。

### 1.3 时间估算与恢复

- `src/codepilot/graph/validation_time.py`：只使用与当前候选身份匹配的样本；建议余量只作诊断，不作事实下界。历史 250s、建议 320s、runner cap 300s 时仍派发，最终 300s 生效。
- `src/codepilot/graph/deps.py` 的 `AccountingSandbox.run_tests`：资源槽 → identity → estimate → `dispatch_timeout(min(env.timeout_seconds, suggested))` → `check_dispatch("test")` → 计数 → 内层执行 → 写 timing facts。取得资源后再次核对剩余时间；未派发不增加测试次数、不写样本。
- 候选变更使旧慢样本失效；文件级/聚合样本只作 aggregate，不把不完整范围升级为 complete；Review/closure 预留只扣一次。
- 恢复：取消后从有效 checkpoint 继续；同候选只补缺失验证；official-only 用假 runner，不调用内部推理；无效结果保持可恢复；有效结果不重复执行；账本与历史不回写。

验证：`tests/unit/test_phase26_estimation.py` **5 passed**：候选变更仍派发、余量不足仍派发 300s、未派发不计数、文件级 selector 记 aggregate、closure 预留只扣一次。恢复路径由 ownership 5 tests 与 `test_resume.py` 覆盖。

## 2. 最终回归

隔离启动器 `.venv/Scripts/python.exe .codex-pytest-tmp/phase26-runtime/isolated_pytest.py` 清除凭据类环境变量、阻断非回环 socket，并自动追加 `-m "not docker and not llm"`。日志位于 `.codex-pytest-tmp/phase26-runtime/`。

| 组 | 范围 | 结果 | 日志 |
|---|---|---|---|
| A | Phase26 机制、deadline、closeout/final/harness、取消、deps、事件、账本对账 | 113 passed，71.91s | `final-A-group.log` |
| B | checkpoint 恢复、索引、批次 | 85 passed，179.96s | `final-B-group.log` |
| C | `tests/unit/llm` 全套 | 439 passed，41.36s | `final-C-llm.log` |
| D | 前端 vitest | 14 files / 50 tests passed | `final-D-vitest.log` |

A 与 C 有两个文件重叠，**不合并成覆盖总数**。早前分组结果基于非最终源码，已被上述运行取代，仅保留日志。

失败处理：`test_deps.py` 旧夹具缺 `names()/query_cache/model_dump`，改为真实 `ToolRegistry()` 与 `EnvironmentBuildResult(status=PASS, image_ref="img", image_id="img-id", cache_key="cache-key")`；`test_phase13_cancel.py` 的 `_guard` 缺 lifecycle/deadline/budget，新增完整 `_guard_deps`；`test_dispatch_ledger_reconciliation.py` 的 `_Exploding` 改为在预留落盘后抛 `ConnectError`。未弱化断言、未删除测试、未加超长 timeout。

## 3. 性能真实观测

小型合成仓库 81 files / 580 symbols / 694 edges；数据见 [phase26-performance-results.json](phase26-performance-results.json)。

- 冷索引 0.923s；热索引（无变更）0.1931s，parsed=0。
- 单文件变化 0.4496s，parsed=1、changed=1、reused=80，边数仍 694。
- read_file：miss 0.01485s、hit 0.00582s；scoped search 首次 0.27579s、二次 0.13672s（hit=false，无可信 revision 时按设计旁路）。
- QueryCache：1 hit / 4 misses / 1 bypassed scope。
- 同 workspace 两任务 0.511s 串行；不同 workspace 0.205s 并行。

结论仅限该合成仓库：无负收益证据，但不可外推到大型仓库；搜索缓存旁路是有意保守行为。

## 4. 页面端到端

真实前端 + FastAPI + FakeLLM + 假 Sandbox，全部回环，结果见 [phase26-page-e2e-results.json](phase26-page-e2e-results.json)，截图在 `.codex-pytest-tmp/phase26-runtime/page-e2e/out/shots/`。6 个场景通过：queued cancel、completed run（succeeded / m2_verified / 174 events / target tests PASS）、running cancel（点击后 2ms 观察到 requested，终态 confirmed）、SSE 重连（175 事件严格递增、无重复、stream_end、after_seq 过滤）、run 切换隔离。console errors 为 0，真实模型请求 0。

首次失败证据保留于 `page-e2e/first-attempt/`（`FINAL OUTCOME` 受 CSS text-transform 影响）；另修复 SSE 解析（sse-starlette 使用 CRLF）与 Run 标签定位（HarnessStatus 仅在 DetailPanel 的 run 标签挂载）。已修改 `HarnessStatus.tsx`，使取消在途时显示 requested，避免与头部 "Cancelling…" 不一致，并补 2 个前端用例。页面验证用最新 `npm run build` 产物；`web/dist` 被忽略，不提交。

## 5. 静态检查

57 个源码文件（50 Python + 5 TS/TSX + 2 locale JSON）：

- AST：50 Python 文件，0 失败。
- Ruff check：All checks passed（首次 5 个错误均在 `test_http_total_deadline.py`，修复后复跑）。
- ruff format --check：50 files already formatted（首次 3 个文件需重排，已格式化）。
- mypy（42 个 src/scripts 文件，`--follow-imports=silent --no-incremental`）：Success。
- TypeScript `tsc --noEmit`：通过；ESLint `--max-warnings=0` 5 个文件：通过。
- `git -c core.whitespace=cr-at-eol diff --check`：通过（普通 `git diff --check` 会把 CRLF 的 CR 误报为尾随空白，未改写换行）。
- locale JSON 解析通过；候选 diff 可反向应用，57 个 diff 路径与 57 个源码路径一致。

## 6. 归档与本地提交

`configs/harness/candidates/phase26-candidate-v5` 含 `candidate.json`、`snapshot.json`、`sources.json`、`candidate.diff`：57 个源码文件，父版本 `phase26-candidate-v4`，status=pending，无 active 指针。

- diff：345172 字节，SHA256 `b2d0cbfd8c9a8b88459bd36b6a16fea3fd55ac5dbe9839095a1c1614f6c69a52`。
- snapshot：SHA256 `ecf503d292a5a030514ed7f31926180b6d230b88251e616e026dc15ecfbfd052`。
- 源码：LF 规范化哈希与原始字节哈希均与工作树一致；反向应用校验通过。
- 6 个归档版本（baseline 与 v1–v5）内部哈希 259 条全部核对通过，旧版本保留。
- 本地提交仅含本任务源码、测试、文档与候选档案；不 push、不建 PR。

## 7. 未执行项与仍待验证

未执行：test50/test300/M4、Docker/镜像/官方 Harness、真实模型或服务商请求、RSI 效果实验、自动提升策略、push/PR/发布。

仍待真实环境验证：真实服务商流式延迟与账单对账、真实 Docker/官方 Sandbox 行为、大型仓库索引与缓存收益、长时间运行下的线程/连接稳定性。页面验证使用 Fake provider 与假 Sandbox，不能替代真实模型端到端。

## 8. 下一阶段条件

1. 在获得明确授权与真实凭据隔离方案后，用固定模型/provider/预算跑小样本，核对真实 usage 与 loopback 假服务的 admission 结论一致。
2. 在隔离环境验证 Docker/官方 Harness 的 timeout 与资源关闭。
3. 在更大仓库复测索引与缓存，确认无负收益后再讨论默认策略。
4. v5 保持 pending；只有独立效果比较后才讨论 active 指针与默认版本提升。

## 9. 历史中间记录（旧结论已失效）

本文件旧版把 HTTP 总 deadline、所有权、取消恢复、性能、页面、静态检查、v5 与提交列为未完成，该结论现已被本轮结果取代。历史证据仍在：`.codex-pytest-tmp/phase26-runtime/` 下的 `final-current.log`、`hf.log`、`reg-current.log`、`batch.log`、`batch-2.log`、`frontend.log` 等保留首次失败与修复过程；`docs/notes/phase26-final-mypy.txt` 与 `phase26-final-check-results.json` 保留上一轮静态输出。隔离器曾遇到 Windows 临时目录权限拒绝与 spawn 递归，已用入口保护修正，未削弱断言。
