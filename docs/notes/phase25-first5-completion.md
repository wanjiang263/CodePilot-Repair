# Phase25：固定 test50 首组 5 题

用户将本轮运行范围明确调整为“先完成首5题，再评估”，因此保留原固定 catalog 和顺序，仅运行前五题。其余45题不记为 NO_PATCH 或 UNRESOLVED，也不自动进入下一批。三道人工作业调试题独立统计。

## 首组最终结果

首5题真实派发全部完成。4个合法最终候选全部取得有效官方判分，无遗漏候选；第五题没有补丁，因此没有提交空补丁冒充评测。

| 固定顺序 | 实例 | 内部结果 | 官方结果 | 请求数 | 费用 CNY | Agent运行秒数 |
| --- | --- | --- | --- | ---: | ---: | ---: |
| 1 | sphinx-doc__sphinx-8506 | succeeded / ACCEPTED | UNRESOLVED | 25 | 0.16177800 | 668.775 |
| 2 | django__django-14999 | failed / tests_failed；同候选补验614项通过 | RESOLVED | 39 | 0.27541720 | 2289.786 |
| 3 | astropy__astropy-14995 | succeeded / ACCEPTED | RESOLVED | 20 | 0.16830208 | 405.512 |
| 4 | astropy__astropy-6938 | stopped / repeated_patch，内部REJECTED | RESOLVED | 24 | 0.14407392 | 1024.844 |
| 5 | django__django-16816 | budget_exhausted / budget_tool_calls | NOT_EVALUATED：NO_PATCH | 45 | 0.30942112 | 1038.645 |

上表费用包括付费环境恢复段；Agent运行秒数汇总各已记录 report 的 `runtime_seconds`，不包含预置镜像、初始索引及独立官方评测的全部墙钟时间。Django-14999 初始完整索引1022.063秒、Django-16816索引433.359秒，其他逐段索引耗时见原始 smoke 日志；不把跨夜等待算作模型运行。每题精确候选SHA256、官方文件、账本来源及恢复记录见结果JSON。

第五题 Coder 的30次工具调用全部是只读操作：15次search_code、14次read_file、1次get_git_diff；29次成功、1次模型生成的JSON参数非法。达到事先配置的工具上限后工作树无diff、最终状态无current_patch，因此NO_PATCH有实际依据。没有为得到通过答案手工改写该未见题、没有因官方反馈追加答案策略。

统计：派发覆盖率5/50=10%；官方判分覆盖率4/50=8%，或4/5已派发=80%；合法候选判分覆盖率4/4=100%。RESOLVED/50=3/50=6%（这是原固定catalog覆盖口径，不能当作完成50题后的解决率）；首组解决率3/5=60%；RESOLVED/有效官方判分=3/4=75%；NO_PATCH=1；最终环境无效=0；最终系统错误=0。此前环境与系统故障及其恢复没有从记录中删除。

按推理版本分组：`9ef018d` 1题、1个官方判分、0解决；`45a0e4e` 2题、2个官方判分、2解决；`80b453a` 2题、1个官方判分、1解决（含14999恢复段和16816）。14999初次失败属于旧版本边界，费用与证据仍计入该题。`45298a3` 是之后的解析归因修复，仅用于相同日志分析和无付费补验，没有追加test50模型派发。

## 费用与是否继续

本轮新增 settled **1.05899232 CNY**，新增unknown预留 **0**，共153次请求。三题人工调试没有新增模型调用。原30元授权此前 settled=1.57469432、unknown预留=1.62293760，合计上界3.19763192；加入本轮后累计上界 **4.25662424**，余额 **25.74337576 CNY**。历史unknown完整保留，不重复收费、未改零。

全部历史来源（包括更早授权范围）opening settled=8.75813592、unknown预留=5.68028160（7次）；加入本轮后的跨历史总上界15.49740984。这个较大opening不能再完整叠加到原30元口径，否则会重复扣除旧授权。审计脚本验证全部历史来源哈希，按唯一新账本求和，忽略各执行段复制的opening金额。

建议本轮到首5题闭环结束，先利用结果改进内部回归的时间分配和Coder反复只读导致无补丁的问题。若目的是工程可用性验收，当前首组已经提供有效闭环证据；若要报告稳定解决率或比较版本，需要继续原固定集合，5题且中途有版本修复不足以支持总体性能结论。继续45题的主要代价是运行和环境维护时间，当前费用余额足够；后续应明确一次性冻结版本，沿既有顺序继续，不能因目前3/5结果挑选样本。剩余45题本轮最终记为 `NOT_RUN_USER_SCOPE_CHANGED`。

## 身份与证据

- 原固定 catalog：`artifacts/phase22/next-stage/catalog.json`，SHA256 `3cfa3b13cbbf281aafb216a6942bfbdc4c249c5c082c715218ea786c2cc0c3be`。
- 数据版本：SWE-bench Lite，revision `6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2`。公开任务使用 inference loader；没有读取参考补丁或把官方隐藏测试正文送入 Agent。
- 模型：BigModel `glm-5.3-flash`，配置别名 `glm-5.3-flash-main`；temperature=0；HTTP最多一次请求，没有隐藏重试或更换服务商。
- 逐题结果和账本哈希：`artifacts/phase25/first5-results.json`，便于查看的表格：`artifacts/phase25/first5-results.csv`。原始运行目录及官方评测文件在每行链接中。
- 原始工作树中的用户未跟踪文件未删除。没有 push、PR、test300 或 M4。

## 工程修复

`m3_batch.py` 区分实例错误与批次错误。workspace、单题测试和 Harness 错误保留异常栈及可恢复状态，继续独立实例；总预算、全局身份和当前执行段冻结等全局条件停止派发。恢复时复用已完成推理和合法候选，官方重评不重新调用模型。即使内部 Review 拒绝，合法最终候选仍提交官方评测。

真实 CLI 故障探针在第一题触发不存在的 workspace seed，第二题仍进入预算检查，再被零预算正确阻止；共0次请求。证据：`artifacts/phase25/fault-probe/`。单元测试另覆盖首题 workspace/Harness 失败继续、预算停止、恢复不重复付费、unknown保留和新段总额约束。

WSL 中 `/mnt/d` 的 bind source 原先没有映射到 Docker Desktop daemon 实际可见的驱动器路径，导致工作区/结果目录绑定为空；现在按明确的 Desktop guest socket 映射到 `/run/desktop/mnt/host/d`。真实绑定测试能产生逐测试结果，拒绝0 tests假通过。

Astropy 绑定源码需要镜像已编译产物；恢复缺失的 `.so` 和生成元数据/ERFA wrapper，保留已有源码。Astropy-6938 修复后真实基线84 passed、0 failed、2 skipped。Reproducer 对函数选择器识别实际参数化子项；显式参数选择器仍需精确匹配，不放宽缺失测试检查。

Django 镜像缺少 pytest，派生镜像只补 pytest7.4.4，保留父镜像层和代码身份。14999 的恢复复用完整索引、已结算定位阶段和相同输入响应缓存能力，旧费用保留；16816 直接使用冻结的派生镜像。并发限制为两条推理，避免此前内存不足造成无付费索引中断。

Django-14999 最终合法候选官方 RESOLVED。原内部状态 `failed/tests_failed` 是扩大回归的时间预算中断：614项中执行到228项，缺失386项，被旧解析器误标 collection error。修正后的解析对同一原始日志归因为 TIMEOUT；在候选不变、无新增模型请求的条件下，延长单次回归时间至600秒补验，614项全部通过。证据：`artifacts/phase25/django14999-recovery/postrun-validation/`；原始内部 report、账本和官方候选均未覆盖。

## 三题调试结果（不计入 test50）

| 实例 | 根因及修正 | 实际验证 | 新版官方结果 |
| --- | --- | --- | --- |
| sphinx-doc__sphinx-8273 | 开关启用后的目录契约是 `1/`、`3/`；旧候选错误生成 `man1/` | 真实构建验证关闭为平铺、开启为两种 section 子目录，回归保留原有基线 caption 失败 | RESOLVED |
| matplotlib__matplotlib-18869 | 旧候选漏掉 `_parse_to_version_info`，返回结构也不符需求 | 实际接口返回带 major/minor/micro/releaselevel/serial 的命名元组；final/rc/dev/post/local/beta、比较和原版本获取均验证；相关回归3 passed、1 skipped | RESOLVED |
| psf__requests-3362 | 无编码流需惰性增量解码；旧检测可能缓存整个输入；官方超时目标在当前网络可达 | 限制检测前缀为64KiB，保留已读块并增量解码；单次流不预读，分块多字节和后续UTF8真实验证；100MiB生成源首个输出在64KiB后出现 | RESOLVED |

调试补丁和日志：`artifacts/phase25/debug/{sphinx,matplotlib,requests}.diff` 及相邻日志。三个 `manual-debug-phase25.*.json` 保存官方原始结果。没有把这些已见反馈样本算作独立泛化成绩。

Requests base和候选在相同环境中都出现两个连接超时测试失败。经用户明确授权，临时仅丢弃 `10.255.255.1:80` 流量后，两边这两个测试均通过；官方复评得到 RESOLVED。临时规则已撤销，证据 `tarpit-rule-removal.txt`。完整185项回归的 base/候选状态一致（180 passed、2 failed、1 error、1 xfail、1 xpass）；残留错误属于原环境/依赖问题，不宣称全185项通过，也不改写旧官方 verdict。

Requests 的取舍：编码检测只看前64KiB，遇到超长前缀后的不同编码可能判断不准；ASCII前缀按UTF8增量解码，错误替换。首个输出可能等待检测完成或64KiB；保留的数据上界为检测前缀加当前调用产生的一块，调用方给出巨型块时该块自身仍可能很大。验证记录这一行为，避免宣称与整段 `apparent_encoding` 在所有输入上等价。

## 验证与提交

相关测试149 passed（`artifacts/phase25/delivery-tests.log`、`delivery-tests.xml`）。修改的10个源文件/测试文件 Ruff check 和 format check通过，6个源文件 mypy通过；没有无关全仓格式化。随后实际运行暴露原生 runner 将超时中断的缺失测试优先归因为 collection error，已调整为保留部分结果和缺失列表、以真实 timeout 为主因；新增4种验证情形，原生 runner 17项测试通过，相关 Ruff/format/mypy通过。

本地实现提交：

- `3ffccccd932e18d66c01c787ad8695077eb8f5cd`：批次隔离、恢复和运行工具。
- `b4a0ebd0197da3ea64fceec1a38aba54857ae150`：WSL绑定路径。
- `9ef018d70ecf9346896557ce6c19e5a2f2925f8d`：Astropy编译产物。
- `45a0e4e9d4d6c5ad99e242e2293c4c44895497d9`：参数化复现测试和ERFA wrapper。
- `80b453a30a93775f3f51dad96b76042aabcb8995`：镜像配置冻结、官方恢复和准确费用。
- `45298a3`：Django/Sympy原生测试超时归因，保留缺失测试诊断；原已运行的结果不回写。
- `a7ef2c1`：三题调试补丁、官方RESOLVED及网络环境对照证据。

旧无付费索引中断、Django环境失败及每段版本边界保留在原始证据中。最终按模型实际请求计费，索引耗时与失败环境恢复耗时另见逐题记录。

收尾状态：Requests临时防火墙规则已撤销；Aegis容器原重启策略已按记录恢复，恢复时仍保持Paused（`artifacts/phase25/aegis-restart-policy-restored.txt`）。2026-10-28 09:50左右最终核查无本任务后台Python/WSL进程，Docker Desktop Linux Engine管道不存在，未为交付重新启动Docker。旧证据、镜像、账本和用户文件保留。没有push或PR。
