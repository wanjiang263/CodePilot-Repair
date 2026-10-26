import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
BASE = ROOT / 'artifacts/phase22'
r = json.loads((BASE / 'results.json').read_text(encoding='utf-8'))
c = r['cost']
cov = r['coverage']
lines = ['# Phase 22：原六实例真实恢复结果', '', '日期：2026-10-26。唯一源码根：`C:\\Users\\lenovo\\Desktop\\CodePilot-Repair`；WSL 使用同一目录 `/mnt/c/Users/lenovo/Desktop/CodePilot-Repair`。所有金额为 CNY。', '',
f"实际付费主尝试 **{cov['dispatched']}/6**；可信终态 **{cov['trusted_terminal_records']}/6**；内部 ACCEPTED **{cov['internal_accepted']}/6**；有效官方判分 **{cov['valid_official']}/6**。批次状态 `{r['batch_status']}`。这些是已见实例恢复验证，不代表未见泛化能力。", '',
'## 逐题结果', '', '| 实例 | HTTP 次数 | 内部终态 | 官方 | 本轮新增费用 | Graph 秒 |', '|---|---:|---|---|---:|---:|']
for x in r['instances']:
    report = x.get('workflow_report') or {}
    lines.append(f"| {x['instance_id']} | {x['http_requests']} | {x['internal']} | {x['official']} | {x['phase22_settled_cny']:.8f} | {report.get('runtime_seconds', '未完整退出')} |")
lines += ['', '每题最多一次付费主 Graph；Graph 内多次工具/模型请求不是第二次主尝试。执行顺序 Sphinx → SymPy → Matplotlib → sklearn → Requests → Django。所有原始记录保留；最终归一化结果及 SHA 见 `artifacts/phase22/results.json`。', '',
'- **Sphinx**：派发后发现 shared registry factory 忽略冻结的 timeout/trust_env。两次请求都已结算后停止进程，保留 SYSTEM_CONFIG_MISMATCH；修复后只继续未派发题。',
'- **SymPy**：MRO 索引性能问题在派发前修复；真实主 Graph 完成目标测试、完整回归和一次 Reviewer，但实际 Reviewer 输入遗漏完整补丁，旧 approved 不可信。新增 `review_context_bound` 严格验收后 REJECTED，未付费重审，未提交官方。',
'- **Matplotlib**：localizer 返回空文件集，最终没有授权目标测试，补丁从未完成目标闭环，REJECTED。真实 HTTP 已发生；原验收 real_run=false 表示缺少目标执行证明，不能解释为零模型。',
'- **sklearn**：目标、完整回归、独立 Reviewer 均通过；实际 Reviewer 请求包含完整候选补丁，validated/tested/reviewed/delivered/prediction 身份一致。官方有效 RESOLVED。',
'- **Requests**：复现 base workspace 的 packed tree 对象写入失败，内层 sandbox_error 被外层 NO_PATCH 掩盖；最终记 ENV_ERROR。修复 Git 原始对象读取、base-only copy 和停批判断，真实 Requests/Django base-copy 轻测均 PASS；未重跑 Requests。',
'- **Django**：旧未知费用原样继承；本轮独立 run/ledger，仅一次新主尝试。其实际 closure、失败原因、预算、测试次数和官方记录见下表后的逐题证据。', '', '## 逐题可追溯证据', '']
for x in r['instances']:
    report = x.get('workflow_report') or {}
    lines += [f"### {x['instance_id']}", '', f"- run_id：`{x.get('run_id')}`", f"- 报告：`{report.get('path')}`；SHA256 `{report.get('sha256')}`", f"- stop_reason：`{report.get('stop_reason')}`；failure_reasons：`{report.get('failure_reasons')}`", f"- test_runs：`{report.get('test_runs')}`；effective budget：`{json.dumps(report.get('budget'), ensure_ascii=False)}`", f"- closure：`{json.dumps(report.get('closure'), ensure_ascii=False)}`", f"- 官方证据：`{json.dumps(x.get('official_record'), ensure_ascii=False)}`", '']
lines += ['## Provider 调查及冻结配置', '',
'Phase21 Django 第五次失败是 **31.921 秒 RemoteProtocolError**，不是已证实的 120 秒 ReadTimeout。旧客户端四类 timeout 均为 120 秒；第五次 input usage 未知，不能写 0。旧请求体未保存精确 wire bytes、响应头、服务端 request_id 和完整异常链，无法事后重建。旧已知输入 token 为 14775/24554/5132/6263，输出上限 4096。代理是候选原因；服务端、网络和进程原因没有足够证据定论。休眠事件检查未发现临近电源事件，但这不证明网络正常。', '',
'本轮冻结 alias `glm-5.3-flash-main`，实际 request model `glm-5.3-flash`，temperature=0，HTTP/SDK retries=0。connect/read/write/pool = **10/120/30/10 秒**，`trust_env=false`；Graph 1800 秒，Reviewer 预留 240 秒。没有盲目延长 read，没有切换模型，真实诊断请求 **0**；仅无费用 TLS 检查（TLS1.3，0.422 秒）。Sphinx 首段实际 factory 仍用了旧默认，不能以期望配置冒充实际配置；9f8fc16 后真实客户端 timeout/trust_env 已记录验证。', '',
'每次派发前持久化预扣、请求 SHA、purpose、角色、请求体大小估计、客户端实际 timeout/trust_env、耗时、异常类型链/timeout 阶段、可用响应 request_id/status。`request_body_bytes` 是 JSON 序列化估计，不是经测量的精确 HTTP wire bytes。不得记录凭据。Mock 覆盖读超时、协议错误、取消/中断后冻结和停止。新请求未知立即冻结，不能自动开新账本绕过。', '',
'## 系统修复与执行能力', '',
'六题镜像/runner/scope 身份及轻量真实测试均通过，见 `environment-summary.json`；不把零模型轻测称为完整 Agent 成功。索引仅在 SQLite integrity、repo/base/source/file hash 完全一致时复用。SymPy 通过 immutable resolver 缓存 name/bases/MRO，真实完整 1944 文件索引 694.063 秒；修复前 profile 的 996 样本中 541 个含 MRO，修复后为 0。', '',
'Reviewer 从单个超大 facts 改为紧凑事实、必要源码片段与不可截断的完整补丁；请求必须实际包含完整补丁和计划事实，否则在派发前失败。验收再次验证实际保留请求和候选 SHA；SymPy 历史假通过已明确撤销。普通 RunReport 的 effective budget 从 run 身份绑定的外层报告读取，max_test_runs=18。', '',
'packed tree 缺陷的实际对象 `5602c48ac5ca0cabc7d13e5fddc339410dc7af18`：`cat-file -p` 产生人类可读 tree，重新 hash-object 失败；改为按真实类型读取原始 bytes 并校验对象 SHA。benchmark base provider 使用已验证的一提交干净缓存 copy。内层 sandbox_error/环境错误现在强制 ENV_ERROR 并停批，不能被 NO_PATCH 外层吞掉。', '',
'批次停机对所有未派发题写 NOT_DISPATCHED，对已派发题写真实停止状态；区分空账本中断和付费中断。新增进程锁、PID/退出码/日志、已有 eval 拒绝覆盖、manifest/config/code/budget 身份校验。后台 detached 会话丢失发生在派发前，随后使用保留的 foreground supervisor。所有有费用的系统停止只修复后续未派发题，不重跑已付费题。', '',
'Windows Python 无法启动 WSL（E_ACCESSDENIED）；sklearn 首次启动失败没有启动 Harness、没有伪造 UNRESOLVED。随后通过工具 PowerShell 在同一 WSL 项目目录进行一次实际 Harness，得到有效 RESOLVED。官方缺记录、启动错误、ERROR/INVALID_ENV 均保留。外部 ingest 只读取已生成结果，不再付费派发。', '',
'历史 Phase21 原件不改；`phase21-final-supplement.json` 明确 Django DISPATCHED_STOPPED/PROVIDER_USAGE_UNCERTAIN，后五题 NOT_DISPATCHED/BLOCKED_BY_FROZEN_LEDGER，官方均 NOT_EVALUATED。', '',
'## 费用对账', '',
'历史 opening 11.24078560 = 已结算 7.18344160 + 历史未知预留 4.05734400。原 30 元授权从 Phase20 计算，不把上述历史 opening 再计入新增授权。Phase20 新增 0.25156496；Phase21 新增已结算 0.04758760 + Django 未知预留 0.81146880；Phase20+21 上界 **1.11062136**，来自实际 ledger hash 复算。', '',
f"本轮新增已结算 **{c['phase22_settled_cny']:.8f}**，新增未知预留 **{c['phase22_unknown_reserved_cny']:.8f}**；原 30 元授权累计上界 **{c['original30_used_upper_cny']:.8f}**，余额 **{c['original30_remaining_cny']:.8f}**。本轮 24 元上限余额 **{24-c['phase22_settled_cny']-c['phase22_unknown_reserved_cny']:.8f}**。包含历史的总上界 **{c['all_history_upper_cny']:.8f}**。各题新增均低于 4 元；没有真实诊断费用。", '',
'旧 Django frozen ledger SHA256 `348d46f251f73f22cc207fda8374ca49b40a85d453199d2a6a1318daa439a046`，旧字节原样保留；0.8114688 只是继续预留，不声称服务商已结算。每段 manifest 引用前段 SHA，opening 只继承一次；本轮金额只累计各请求 cost，不重复累加 ledger opening。详见 `opening-audit.json`、`results.json`、各冻结 opening/config 和 `final-audit.json`。', '',
'## 指标与准入', '',
f"官方覆盖率 {r['metrics']['official_coverage']:.2%}；有效官方解决率 {r['metrics']['valid_official_resolution_rate']:.2%}；任务级解决率 {r['metrics']['task_resolution_rate']:.2%}；内部闭环通过率 {r['metrics']['internal_closure_rate']:.2%}。本轮 Provider 未知停止率 {r['metrics']['provider_stop_rate_per_dispatched']:.2%}；另有 Sphinx 配置缺陷停止 1/6。环境/工作区失败率 Requests 1/6=16.67%，镜像轻测失效率 0/6。系统缺陷造成的停止不计为任务解决。", '',
f"下一阶段准备门禁：**{'通过' if r['next_stage_admitted'] else '未通过'}**。运行时观察到的系统问题已修复并有针对性验证；不以已付费题重复执行证明修复。" ]
if r['next_stage_admitted']:
    lines += ['', '已准备 `artifacts/phase22/next-stage/`：50 题未见候选、保守排除集及来源、固定 seed=20261026 的 SHA256 抽样、逐题只读镜像与 runner 覆盖审计、费用估算和运行规格。样本不按结果挑选，不读取隐藏 patch/test_patch/FAIL_TO_PASS/PASS_TO_PASS/hints。未缓存镜像和未核验 runner 明确列出，不能把准备完成称为 50 题环境全部可用。', '', 'test50 状态 PREPARATION_ONLY；没有启动任何 test50/test300/M4。拟议硬上限 50×4=200 元只是后续规格，需要未来明确预算及派发授权；当前原 30 元余额没有自动拨给 test50。六题均值外推仅为偏低的小样本估算，不是费用保证。']
else:
    lines += ['', f"最小剩余阻塞：`{r['stop_reason']}`。未自动准备/启动扩大付费批次。"]
lines += ['', '## 验证与本地提交', '',
'受影响门禁日志均保留于 artifacts/phase22；以下分组有重叠，不能相加当独立测试数：', '',
'| 验证组 | 结果 |', '|---|---|', '| initial-gates（Provider/预算/closure/registry 等） | 214 passed |', '| Provider 独立组 | 56 passed |', '| native runner/M3/acceptance/prediction | 90 passed |', '| dispatch timeout/异常/取消/实际 client factory | 15 passed |', '| snapshot identity/content/hash | 4 passed |', '| MRO/name resolution | 69 passed |', '| Reviewer 完整输入及 budget/closure | 95 passed |', '| official missing/invalid handoff | 27 passed |', '| packed workspace 与 inner sandbox stop | 51 passed |', '| workspace 全相关组 | 91 passed，初始 2 Docker skipped |', '| 上述 2 个真实 Docker 测试单独执行 | 2 passed，9 deselected |', '| 真实 Requests/Django base-copy native probe | 2 PASS |', '| mypy | 198 source files success |', '| 相关 Ruff lint/format | passed |', '',
'没有削弱已有测试断言、删除目标或降低验收标准。相关实现用新增反例阻止假通过。最终额外门禁见 `final-hardening-tests.log`。', '', '| 提交 | 内容 |', '|---|---|']
log = subprocess.check_output(['git', 'log', '--format=%h%x09%s', 'f5458f9..HEAD'], cwd=ROOT, text=True)
for line in reversed(log.splitlines()):
    sha, title = line.split('\t', 1)
    lines.append(f'| `{sha}` | {title} |')
lines += ['', '冻结运行代码依次为首段 6b46e0a、SymPy d5fa740、Matplotlib/sklearn fb4630c、Requests b67e37f、Django f329f3b。最终文档与准备提交发生在运行结束后，不冒充运行时版本。只本地提交；没有 push、PR、历史删除或重写。', '']
(ROOT / 'docs/notes/phase22-six-instance-live-recovery.md').write_text('\n'.join(lines), encoding='utf-8')
print('report written')
