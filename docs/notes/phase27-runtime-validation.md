# Phase27 真实环境验证与交付（2026-10-29）

本轮已完成。基线为 `748ad2f3c882dbba4caafa19bb3fdb1c2ee7982d`，源码提交为 `1e2c4430aebae7d7cea45f245a5482658ca7eb1f`。新档案 `phase27-candidate-v1` 继承 v5、保持 pending，没有创建 active 指针。机器摘要见 [phase27-runtime-results.json](phase27-runtime-results.json)，逐笔账本、原始响应、失败与复验日志见 [证据清单](phase27-evidence/manifest.json)。

原有适用规则是用户提供的全局 AGENTS.md；向上目录与本项目未发现额外 AGENTS.md。未启用 Superpowers、子 Agent、自动 RSI 或新平台。开始时已跟踪工作树干净，历史未跟踪文件保留。Windows 进程检查和 WSL 检查未发现旧推理负载；用户重启后再次确认，启动时间为北京时间 23:07:41，旧付费进程均已结束。历史账本逐个校验哈希，没有改写历史记录。

## HTTP 修复及验证范围

`http_lifecycle.py` 为两个现有 HTTP adapter 提供同一条请求执行路径：每次实际请求拥有自己的 AsyncClient、异步任务和事件循环；同步接口通过复制 context 的线程桥接，始终 join。deadline 或取消使 HTTP task 取消并等待响应/客户端关闭，再返回调用方；没有超时后弃管的请求线程。共享的同步 client 作为请求构造模板保留，不因某一请求失败而关闭。registry 关闭自己创建的模板客户端，借入的客户端由原所有者管理。

总时限从 provider 准入开始覆盖并发槽等待、预算锁等待、重试退避和 HTTP I/O。原 connect/read/write/pool 上限保留；它们与独立的单调时钟 deadline 同时生效。25ms 轮询检查取消，即使 gzip 尚未产生解码输出，也能取消等待中的 HTTP task。模型请求次数仍在派发边界计数，没有添加 HTTP 隐式重试。

响应体由 httpx 解码一次。重建响应时移除原 Content-Encoding、Content-Length、Transfer-Encoding，再由解码后的正文生成长度，避免重复解压和错误的线缆长度。没有永久关闭压缩。

本地真实回环 socket 验证覆盖：接近期限才返回响应头、随后正文停顿；持续慢流；gzip 正常及慢流；派发前取消；预留后但派发前取消；派发后取消；一个请求取消不影响另一个和后续请求；期限包含并发槽等待；解码后头语义；无遗留 HTTP 线程；settled/unknown/cancelled-admission 分类。OpenAI-compatible 与 native Anthropic 都跑了停顿、gzip 和在途取消。native usage 字段结算另有独立断言。

派发前拒绝不形成 unknown；预留后未派发取消会退款并单列 cancelled admission；派发后无法确认 usage 则保留全部 reservation、冻结账本。修正了冻结错误经预算层包装后 provider.unacknowledged 未更新的问题。新记录还保存返回 model、response ID、usage 与服务商请求头 ID。

支持边界：真实服务商仅验证 BigModel GLM。Anthropic 只有本地协议/网络验证；Bedrock 没有获得本轮 HTTP 改造的保证。MockTransport 保留同步测试注入，不能抢占其阻塞 handler；其他自定义同步 transport 会拒绝执行。私有 AsyncClient 使用 ProviderSpec 的网络配置，注入同步 client 的自定义 TLS/代理传输设置不会自动复制，不在本轮兼容保证内。未压力测试 DNS resolver/操作系统清理停顿或长时间连接行为。请求私有连接池不会复用前一次请求的 TLS 连接，这是此兼容改造的开销限制。native Anthropic cache creation 的专门价格仍不支持。

## GLM 小流量与逐笔费用

沿用 `configs/phase22/llm.yaml`：别名 `glm-5.3-flash-main`，实际路由 `https://open.bigmodel.cn/api/paas/v4/chat/completions`，服务商 BigModel，模型 `glm-5.3-flash`，HTTP 重试 0，trust_env=false，connect/write/pool 上限分别为 10/30/10 秒，正常 provider 总上限 120 秒。按配置省略 temperature；未向模型设置 seed。Agent 的 seed=0 只作本地运行身份。

正常路径先执行 3 笔请求，均返回相同模型身份和完整 usage：普通回复、模型调用 `add(17,25)`、实际执行工具并发送结果后的后续请求。三笔墙钟耗时分别约 2.516、8.453、4.844 秒，输入/输出 token 分别为 17/3、195/17、222/11，合计结算 0.00043400 元。实际返回字段和请求身份见 [正常响应](phase27-evidence/glm-normal-responses.json) 与 [账本](phase27-evidence/glm-normal-ledger.json)。

随后仅做一次受控取消：scope deadline 10 秒，取消在约 0.25 秒触发，客户端约 0.25 秒返回；一次本地传输派发，返回时 HTTP 线程列表为空、共享模板未关闭。远端是否已接受请求、是否继续生成或收费不能确认。该笔完整上界 0.80286720 元保留为 unknown_frozen，没有记零，也没有重复派发该请求。见 [取消结果](phase27-evidence/glm-cancel-result.json) 和 [冻结账本](phase27-evidence/glm-cancel-ledger.json)。

13 笔正常请求均有服务商 x-request-id，10 笔 Agent 请求还直接在账本保存返回 model/usage。项目没有接入服务商按 request_id 查询或账户账单接口，因此只能按返回 usage 与既有价格配置对账，不能声称已核验服务商最终扣款。输入/缓存输入/输出单价为每百万 token 0.8/0.23/2.8 CNY；逐笔重算与账本一致。

| 执行段 | settled 笔数 / CNY | unknown 笔数 / 上界 CNY |
|---|---:|---:|
| 正常与工具探针 | 3 / 0.00043400 | 0 / 0 |
| 受控取消 | 0 / 0 | 1 / 0.80286720 |
| DraftStore Agent 案例 | 10 / 0.04913696 | 0 / 0 |
| 本轮合计 | 13 / 0.04957096 | 1 / 0.80286720 |

本轮 14 笔请求，费用上界 **0.85243816 CNY**，低于 3 元上限。原 30 元授权在本轮前的 settled 为 2.63368664 元、unknown 为 1.62293760 元，余额 25.74337576 元，与旧报告一致。本轮后原授权累计上界 **5.10906240 元**，余额 **24.89093760 元**。原授权 unknown 累计 2.42580480 元。更早其他授权中的历史 unknown 也完整保留；跨历史原有 7 笔 unknown，加本轮为 8 笔，不能把这些跨授权 opening 再整体重复扣入原 30 元。

## 真实 Docker 与官方结果恢复

选择历史已有 Django-14999 候选；未调用模型重新生成补丁，未读取参考答案来修改候选。实例 base 为 `a754b82dac511475b6276039471ccd17cc64aeb8`，补丁 SHA256 为 `7f2804c7f51f3a07bc17b21bfa3b15133cb26f79ea7d4845373d87b868074e23`，prediction SHA256 为 `756eda867283cfae7aa3501ee99b3528a2d6228ef4165c9082211f7979c18e06`。receipt 的 provenance、base、补丁和 prediction 均经现有 verify_candidate 核对。

Docker 最初因 WSL socket 转发握手及主机提交内存不足启动失败；日志具体包含 vpnkit-bridge bad magic 和 paging file too small。没有杀掉用户应用或删除镜像。用户重启后正常启动 Docker 29.8.2，复用 `phase25/django-14999:pytest744`，在本轮独立工作区运行。

| 运行 | 实际 runner 上限 | 实际结果 |
|---|---:|---|
| 可控失败 | 1 秒 | TIMEOUT，0/3 项完成，缺失项保持记录 |
| 同候选恢复 | 40 秒 | 3/3 passed，逐项结果完整，无缺失、OOM 或清理错误 |

两次含容器准备、执行和回收的墙钟分别约 5.16、16.67 秒；不能将它们误当为 runner 忽略 1/40 秒执行上限。测试前后补丁未变。历史同候选 614 项完整回归已有可信结果，本轮复用，没有为凑记录重跑。见 [逐测试结果](phase27-evidence/docker-results.json)。

官方恢复在独立目录实际执行现有 OfficialHarnessRunner：先复制可信报告但缺少身份 sidecar，得到 CACHE_UNVERIFIED 拒绝；随后补回经过校验的历史身份，返回 REUSED / RESOLVED。没有新 Harness 子进程或新官方判分；这是对可信结果的恢复，符合复用要求，不是一次新的 RESOLVED。该路径未构造内部推理，也没有模型请求。见 [首次拒绝](phase27-evidence/official-first-failure.json) 和 [恢复记录](phase27-evidence/official-recovery.json)。历史调试结果不计未见样本成绩。

## 较大仓库索引实测

仅使用上述缓存 Django 的 base 源码创建独立副本 `artifacts/phase27/index/workspace`，没有向 Agent 暴露评测答案。原 seed 未修改；临时变化仅在副本的 `django/utils/text.py` 追加注释。共 5,214 个扫描文件、2,717 个 Python 文件。结果见 [performance.json](phase27-evidence/performance.json)。

| 场景 | 总秒数 | 解析 / 解析复用 | 关联重建源数 | 进程峰值工作集 |
|---|---:|---:|---:|---:|
| 新数据库冷索引 | 104.999 | 2717 / 0 | 2717 | 1.165 GiB |
| 新进程、无内容变化 | 8.965 | 0 / 0 | 0（verified snapshot） | 99.4 MiB |
| 冷索引后同进程单文件变化 | 142.746 | 2589 / 128 | 2717 | 1.236 GiB |
| 新进程单文件变化 | 150.024 | 2717 / 0 | 2717 | 1.212 GiB |

另一次新进程先复用 snapshot 再在同进程改文件，耗时 123.674 秒、解析 2717：仅复用数据库不会加载解析树。它与冷进程中保留 128 个解析结果是两种不同状态，不能混称“热索引”。

首次冷索引真实触发 MemoryError，定位到旧 TEST 边合并同时保留旧 ORM 图、merge 字典和全部待写字典。修正为完整关联闭包直接替换 TEST 边，并每 1000 行写入；仍在原调用事务中，不降低正确性或关联范围。也修正了扫描时新解析条目逐出所有待复用 LRU 条目的抖动：固定扫描入口的有界缓存视图，保住 128 个真实解析命中。独立 130 文件用例验证此行为。

第二次冷索引已在 148.203 秒完成，但 Windows 资源统计句柄被截断导致指标脚本失败；保留原日志，修正句柄后用新数据库补齐上表冷索引及峰值指标。重启和系统负载不同，因此不据此声称优化提速百分比。

单文件更新比新数据库冷索引更慢；解析复用不等于关联局部重建，旧图删除/重写也有成本。本轮没有将完整闭包改成未经证明的局部更新，也没有扩大解析缓存来换取未经评估的内存风险。峰值工作集是进程指标，不是整台机器的提交内存。

文件读取 miss/hit 约 13.83/4.52ms，后者确有内容身份命中。范围搜索两次约 281.90/162.95ms，均为缓存旁路，第二次并非搜索缓存命中；首次身份扫描约 62ms，第二次复用旁路决策，未重复扫描大范围身份。没有固定提速倍数承诺。

## 一个真实 Agent 开发案例

在独立本地 DraftStore 小仓库冻结 README 契约、初始提交、4 个公开验收用例、镜像 ID 和预算，再运行现有完整 InferenceAssembly。案例同时要求定位 stale render 缺陷并保留 prefix 的位置/关键字接口、其他键、顺序、快照独立性和异常类型。没有人工代写候选答案，也未使用剩余 test50 题。

Agent 的实际行为：2 次 localizer、3 次 planner、4 次 coder、1 次 reviewer 模型调用；模型工具响应共 5 次 read_file、1 次 edit_file、1 次 run_tests、1 次 finish。Coder 先读源码与测试，再自行在 update 中使该键的所有 prefix 缓存失效，只修改 `drafts/store.py` 两行。实际 Coder 请求录制中包含 change/preserve/interface 契约，首次有效修改约在图开始后 **73.22 秒**；Coder 没有重复查询或低收益警告。没有据单题推断复杂仓库上的行为。

基线 3 passed / 1 failed；目标 4 passed，L1/L2/L3 回归完整、无新增失败；Review 批准，无 high issue。总计 5 次测试运行，形成合法候选，状态 succeeded / m2_verified。外层 assembly 墙钟约 **95.59 秒**，图报告计时 **93.82 秒**，两者边界不同。10 次请求全部 settled，0.04913696 元。见 [冻结案例](phase27-evidence/agent-frozen-case.json)、[真实报告](phase27-evidence/agent-report.json)、[模型调用录制](phase27-evidence/agent-calls.jsonl)、[首次修改事件](phase27-evidence/agent-first-edit.json) 和 [最终补丁](phase27-evidence/agent-patch.diff)。

只完成这一个有限案例；没有可信对照基线，不报告提升百分比或稳定解决率。

## 回归、修正与交付

最终受影响回归：**536 passed，176.47 秒**，覆盖 HTTP/provider/预算/取消/checkpoint、索引和存储。隔离启动器清除凭据并禁止非回环 socket；没有用 skip 或放宽断言通过。首次回归 4 failed / 529 passed：3 项是 registry 默认构造绕过旧 MockTransport 注入点，已恢复注入兼容；另一项是旧九表断言漏掉既有 index_semantics 表，已改为精确十表断言。99 项失败组及相关用例复验通过，最后完整受影响组 536 项通过。重启前中断日志也保留。

Ruff check 与 format --check 通过（16 文件），mypy 9 个受影响源文件通过，TypeScript 和全项目 ESLint 通过，git diff --check 通过。首次 Ruff、mypy 与脚本异常原始证据均保留。没有前端源码改动，因此未重复页面 E2E 或无关前端测试。

本轮修改涉及两个 provider、HTTP 生命周期助手、dispatch scope/预算/重试、registry 所有权、大图边写入与解析缓存、相应测试，以及 4 个可复核探针脚本。完整路径列表在候选档案的 source manifest 中。验证脚本只复现这轮具体工作，没有增加平台、Agent 层级或自动进化机制。

代码取证边界：真实探针和 Agent 是在 `748ad2f` 上逐步修复的工作树中执行，最后才提交为上述源码版本。GLM 探针早于完整 response identity 字段补充；Agent 运行早于最后的 registry 注入兼容修正。最终 536 项回归覆盖收口实现，但没有为获得同一题的最终字节版本记录而重复付费运行。每一执行段的完整源码字节快照未在派发前单独冻结，因此不能将全部真实结果宣称为对最终提交逐字节相同源码的独立重跑；本次候选档案准确冻结的是最终交付源码。

源码本地提交：`1e2c4430aebae7d7cea45f245a5482658ca7eb1f`。报告、证据与 pending 档案另作本地交付提交；最终提交 ID 由聊天交付记录提供，避免文档自引用循环。没有 push、PR、发布、test50/test300/M4、策略自动提升或 active 指针。

封存另发现历史 v2–v5 的 snapshot.json 声明 SHA256 与实际文件不符；实际字节与基线 Git blob 一致，差异在本轮前已存在，并非本轮改写。旧报告“全部归档哈希通过”的断言不能直接沿用。旧文件和声明均保留，逐项差异见 [归档核查](phase27-archive-integrity.json)。新 pending 档案明确记录父 snapshot 的实际/声明身份，独立封存 64 个当前源文件，自己的 snapshot/diff/source 哈希与反向应用检查通过；这不追认旧归档有效，也不激活任何版本。原始证据按字节保存，Git 属性禁用其换行转换，避免记录被检出换行改写。
