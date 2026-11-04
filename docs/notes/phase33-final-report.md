# Phase33 最终报告

结论：CONDITIONAL PASS (corrected in Phase34)

生成时点：C3 (cabf5ef512f14112fe00171ace83d1cc8e3869ad) 之后、C4 之前；本报告所在提交即 C4（回执 + Phase33 文档）。
证据根：.codex-pytest-tmp/phase33/（未跟踪，完整保留）；phase32 历史证据未覆盖、未改写（基线哈希见 evidence/phase32-baseline-hashes.txt）。

## 一、修复结果

### D1 provider（T01）
- scripts/phase32_provider_matrix.py 重写：新增 --real 显式开关，默认仅 fake。真实通道按 provider 分离：openai-compatible（OPENAI_API_KEY + OPENAI_BASE_URL）、anthropic（ANTHROPIC_API_KEY）、bedrock（AWS_ACCESS_KEY_ID/SECRET + boto3/botocore）。
- 预算复用现有 PHASE32_REAL_BUDGET_CNY，上限 20 CNY，无第二预算变量；每 provider 最多 1 次请求、总计最多 3 次、无隐藏重试。
- 状态仅 real-verified / real-failed / blocked 三态；real_requests=实际调用次数；价格未知时 cost_known=false，不编造费用；missing 逐行生成（3 行各自独立列出缺失项）。
- 本轮无预算、无凭据、无用户真实请求授权 → 3 个真实行全部 blocked，real_requests=0，real_cost_cny=0（docs/notes/phase33-provider-matrix.json，sha256 23a798b891b84bf01b05a142634dc7000efd7dbabf5ecc35276e28dc571b6950）。
- fake transport 测试 tests/unit/llm/test_phase33_provider_real.py 12 passed：证明"有预算+有凭据"时真实代码路径被调用且恰好 1 次、fake 结果不会写成 real-verified、无凭据时逐行 blocked 且 0 请求。tests/unit/llm 全组 509 passed。
- phase32-final-report.md 中"设置预算并注入凭据后重跑"在 Phase32 代码上不成立（real_rows 无条件写死 blocked/0/0）。该历史文档按基线保护不改写；本结论由本报告更正并取代。Phase33 起该表述对新 runner 成立：--real + 预算 + 凭据即可发起真实调用（本轮未授权，未执行）。

### D2 test gate（T03）
- scripts/phase32_test_gate.py 中 pytest_temp_root()/clear_pytest_temp() 对全局 %TEMP%/pytest-of-<user> 的枚举与删除逻辑整体移除（lead grep 复核无残留）。
- 每组独立 --basetemp=<SCRATCH>/pytest/<suite>-run<n>-<微秒时间戳>；结束仅 remove_owned_dir 清理本组目录；gate 记录新增 basetemp 与 temp_reclaimed{path,existed,removed,bytes_removed} 审计字段。
- 内存预检、串行执行、rg 路径注入全部保留；新增 --scratch/--summary 参数化（Phase33 全部调用传 --scratch .codex-pytest-tmp/phase33，phase32 目录未触碰）。
- 新增 tests/unit/test_phase33_test_gate.py 5 passed：断言子进程命令包含独立 --basetemp、全局 pytest 根内 sentinel 目录在 gate 运行后仍存在、gate 自有目录被清理。

### D3 runtime JSON（T04）
- 新增 scripts/phase33_runtime_results.py（约 370 行），phase32_runtime_results.py 及其 JSON 原样保留（历史 --check 能力不受影响）。
- 全部 commit 从 git rev-parse / git log base..HEAD 读取（无写死 5e18317）；real_requests/real_cost_cny 读自 phase33-provider-matrix.json；页面 changed_files 由 git diff --name-only b482591..HEAD -- web/src 计算（=0）；docker 结果读自 runs/docker-minimal.done 标记与日志（wall_s=71.26 从 pytest 摘要行解析，非硬编码 310.36）；failed 由每组 JUnit failure+error 计数得出（禁止恒 0）；first_round_failures 从 fa-<group>-run<n>.{log,xml,meta.json} 证据生成，含 exit_code/free_mb_after/nodes/error_text 字段。
- --check 不一致退出 1，仅 --write 写文件。本轮 --write + --check 均 exit 0：docs/notes/phase33-runtime-results.json sha256 f1c37a32a7a29eb831ea172d36fd5b1c8aba5c934f2594d07fd9456467a23330，证据 runs/t10-runtime-check.log（含生成前后 rev-parse HEAD=cabf5ef）。
- 首轮 unit 失败（外部击杀）如实入账：exit_code=1（gate 观测值，meta 内注明 pytest 自身 rc 未被记录）、free_mb_after=1933、summary_line/nodes=null（pytest 未输出摘要、未写 JUnit——如实，不伪造）。
- 多 part gate 输入支持：合并 test-gate*.json 与从 runs/t08-gate*.log 抢救的 record 行，(suite,run,started_at) 去重；unit 四批聚合（batches 不相交可加），archive 重叠口径单独注明。

### D4 索引（T05）
- src/codepilot/repo/index.py 677→872 行（C1 已提交）：符号集变化不再无条件整图删插（Phase32 观测 151822 条边）；改为受影响闭包有界重建——完整上下文 ImportResolver + _affected_closure 四通道（dirty-module 绑定传播含包前缀、同 stem 测试文件、方法名 delta 属性调用扫描、变更文件自身）；抽取 closure-only；替换集 = 闭包符号 ∪ removed_symbol_ids；仅 TEST 且非 fast/非 closure 时走 full replace；快路径 guard 回退到闭包而非整图；IndexStats 新增默认字段 edge_closure_paths=()。解析缓存为进程内 LRU（文件内容 sha256 键）；跨进程磁盘缓存在 parse 阶段仅约 2s 的收益下未引入（REPORT.md §3）。
- 实测（同机同日、pinned b4825911、导出 2417 文件、探针 src/codepilot/eval/swebench_runner.py、每场景独立 DB）：
  - 基线复现（改前代码）：cold 74.37s / nochange 2.98s / change 86.32s / change_body 37.12s —— 负收益复现（change 比 cold 慢 16%）。
  - 修复后：cold 103.89s / nochange 7.32s / change 45.00s / change_body 37.13s；change −47.9%。
  - 验收：change <= cold 且 change_body <= cold 成立 → 快路径默认开启，无需旁路。
  - cold/nochange 修复后膨胀为扫描 I/O 噪声（scan 22.41→36.77s，该路径代码未变；同轮全量 verify_change 73.85s ≈ 基线全量 79.59s，证明全量路径无回归）。
- dump_index 逐字节一致：3 对（nochange-vs-cold、change-vs-verify_change、change_body-vs-verify_change_body）all_equal=true；lead 独立复跑 compare_dumps（change vs verify_change）whole sha256 1ca389fd3f41908c13e83ad9425f24a01bc43416f6a5653b163b6ae0a59c9c46 两侧一致。
- 计数一致：edge_rows 152371 / symbol_rows 10271 与基线复现完全相同；parsed 965、content_reused 2416/2417。change 场景分段：delete 14.77→3.12s、insert 23.54→4.01s、extract_edges 18.90→6.91s、extract_calls 8.61→6.14s。
- 测试：tests/unit/repo 397 passed（lead 复跑 373.07s exit 0；t05 格式化后复跑 412.30s）。test_index.py 33 例含 7 个 Phase33 新测试（只改函数体→fast_mode=True 且符号集不变；新增函数→仅重建受影响边且替换源集==闭包⊊全图；import 改向→guard 回退闭包；reexport 链闭包；删除文件→无陈旧边；delta 通道同名方法；stem 通道）。phase29 契约测试：等价性断言（dump_index[1:]）全部保留，spy 断言改为闭包契约（edge_closure_paths=={hub,leaf,extra} 且 hub_ids⊆闭包⊊全体符号，严格子集），更名 test_symbol_addition_rebuilds_affected_closure_and_stays_equivalent，docstring 重写并记录依据（lead 批准）。
- phase32-final-report.md 标题"负收益与旁路（不是只记录）"与当时实况（仅加 IndexStats.fast_mode 观测）不符，由本报告更正取代；历史文档不改写。
- 证据：evidence/t05/REPORT.md（12374B）、profile-after.json、profile-baseline.json、after-drive.log、baseline-drive.log、6 组独立 DB；汇总 docs/notes/phase33-index-profile.json（sha256 f719f6626ad890bc18294475e0744ccbb83c17b96e52c2a32c5b82a1a4c23dad）。

### D5 稳定性（T06）
- API flake 根因修复（非重跑掩盖）：根因 = Windows 瞬态文件锁（AV/Defender 持句柄 → rename/unlink WinError 5）× 三处零重试实现 + stdlib logging 固定持有 pytest 已销毁的 capture 流；低内存窗口（756MB）放大。
  - src/codepilot/api/demo.py：materialize_demo_repo 的 staging.replace(target) → _replace_with_retry（5 次尝试、退避 0.1/0.2/0.4/0.8s，每次重查 target/'.git' 以幂等复用并发胜者）；_remove 重写为 chmod+重试（旧 rmtree(ignore_errors=True) 会静默残留只读 git objects，diag-3 .tmp-42848 证据）。
  - src/codepilot/api/settings_routes.py：_force_rmtree → 整轮重试+退避（最坏 1.5s）后仍失败则 raise OSError 并列前 20 个残留路径 → 消除 cleanup.json()["deleted"]==[] 链（旧行为：单次 rmtree 失败→OSError→路由捕获→静默 skipped artifact_cleanup_failed）。
  - src/codepilot/observability/logging.py:56：basicConfig stream=sys.stderr → cast('TextIO', _stderr) 复用既有 _LiveStderr 活代理 → 消除 ValueError: I/O operation on closed file（旧 handler 钉住 pytest 瞬态流，teardown 后 anyio portal 线程写 app.py:130 log.info('api.request') 到已死流）。
  - 验证：目标测试 test_runtime_logs_docs_and_scoped_cleanup 20/20 rc=0（9.37–14.19s）×2 轮；patch_report 文件 20/20 rc=0；tests/api 全组 5 轮全绿（118 passed/轮；修复前 113/轮且第 3 轮曾 1 failed）；ValueError 出现次数 0（修复前每轮都有）。新增 tests/api/test_phase33_windows_transient_locks.py 5 个回归测试。未放宽任何断言（test_phase13_patch_report.py 404==400 保留，修实现）。证据 evidence/t06/api-flake-analysis.md（8 节）、runs/t06-verify-loops.status。
- Windows OOM：根因定位 = 系统级 commit-limit 耗尽（git add -A/commit/init 报 fatal: Out of memory，1MB 分配失败；一次 git init rc=3221225773=0xC000012D STATUS_COMMITMENT_LIMIT；内存轨迹 unit#1 3384→1493MB、unit#2 1491→756MB、pagefile 32560→48944MB），非夹具体积（build_repo 夹具=5 个 <1KB 文本文件）→ 未改任何夹具（无杠杆点，如实记录）。隔离措施 = unit 分批提交 + gate 内存阈值（min_free_mb/min_commit_headroom_mb，明确标注为保护而非修复）+ 串行纪律。证据 evidence/t06/oom-analysis.md。
- logging.py 超出申报写范围的说明：它是任务 A 症状（ValueError）的直接根因，单行绑定替换、零 API 变更；lead 批准并在此记录。

### D6 静态/口径（T07）
- scripts/phase32_static_checks.py 全面重写：--base/--scratch/--docs-out/--tool 参数化（phase32 文档与 scratch 不再被覆盖）；mypy 错误以仓库相对全路径规范化为键（修复 basename 碰撞，旧 :130 rsplit 与 :143 by_base）；基线镜像到 <scratch>/mypy-base/<relpath>（无碰撞）；新文件 baseline_errors=None、当前错误全计新增（门禁要求 0）；changed 集并入 git ls-files --others --exclude-standard（新目录未跟踪文件不可逃逸）；全量树 mypy 保留为不门禁的债务记录。
- 新增 locale JSON 检查：web/src/locales/{en-US,zh-CN}/index.json 解析 + 键平价（缺失互列，解析错误才 exit 1）；CRLF-aware diff --check 双跑（core.whitespace=cr-at-eol；plain 失败而 CRLF-aware 通过时注明误报）。
- 修复过程中发现并处理（全部记录 evidence/t07-static/lead-cleanup.md，机械性修改零行为变化）：tests/unit/repo/test_index.py:912 行尾多余空行（git diff --check 唯一真实 EOF 问题，字节级修复 38439→38438）；phase32_index_profile.py 3 个 mypy 错误（method-assign→setattr 变量属性、import-not-found→importlib、no-untyped-def→-> None）；provider_matrix 2 个 mypy（min 类型收窄、probe_provider 参数判空）；多文件 ruff RUF100/SIM105/F401/RET502/RUF043/I001/N818-noqa 等清理；复跑受影响测试 22 passed。
- 最终运行：--base b4825911… --scratch .codex-pytest-tmp/phase33 --docs-out docs/notes/phase33-static-checks.json --tool phase33-static-final → 16 文件、failed=[]、exit 0；产物 .codex-pytest-tmp/phase33/static-checks.json sha256 fbc53e85414baa15483bc1e36244a24d3d5b28fe0067aa656c260c8c3681425c。凭据扫描（仅新增行）0 命中；EOL 检查 LF；全量 mypy 债务另行记录不混入门禁。
- archive 口径修正：archive 组 = 10 文件 + tests/unit/test_phase31_receipt.py 共 11 路径，实测 158 passed = 131 + 27；与 unit-c 的重叠在 runtime-results overlap_note 明示，重叠组不相加冒充总数。

## 二、测试结果

全部组经 gate 串行执行、独立 --basetemp、rg 注入（C:\Users\lenovo\AppData\Local\OpenAI\Codex\bin\rg.exe，ripgrep 15.1.0）、-m "not docker and not llm"、-p no:cacheprovider；PYTHONUTF8=1、PYTHONIOENCODING=utf-8、PYTHONDONTWRITEBYTECODE=1、四个 provider 凭据环境变量 unset。

| 组 | 结果 | 耗时 | exit | log sha256（前16） | junit sha256（前16） |
|---|---|---|---|---|---|
| graph | 266 passed, 13 deselected | 2359.16s | 0 | c5ed4e48b5868ebb | 6a6b011665cddebd |
| unit（a/b/c/d 聚合） | 3089 passed, 2 skipped, 0 failed | 2100.69s（四批合计） | 0 | 见 runtime-results groups | 见 runtime-results groups |
| ├ unit-a | 1533 passed | 272.47s | 0 | e2c322a6c591b455 | a2f54a58aa61c8ee |
| ├ unit-b | 1138 passed, 2 skipped | 433.50s | 0 | 8a2bf69ffb955cc7 | 3f327d58ebf4af45 |
| ├ unit-c | 393 passed | 1359.80s | 0 | 6042a3cd4adb92f3 | 56b2be820e6e9c8b |
| └ unit-d（补漏 tests/unit/tools） | 25 passed | 13.78s | 0 | e19844ec5918f6e5 | 2d17d6b82970105c |
| integration | 126 passed, 1 deselected | 600.36s | 0 | 123e0c73a87a14a7 | 8987929ee24e3c73 |
| api | 118 passed | 236.17s | 0 | b6bf3c9d842a010f | 9f3a5dd056b9e33c |
| archive（11 路径） | 158 passed（131+27） | 1230.60s | 0 | 1f4b9f5e5932cf0a | 596eed9d78c88153 |
| 前端 | 条件跳过：git diff --name-only b482591..HEAD -- web/src 输出为空（如实记录，不伪造通过） | — | — | — | — |

- unit 聚合算术闭环：1533+1138+393+25=3089 passed = phase32 基线 3065 + Phase33 新增 24（provider 12 + gate 5 + index 7）；四批不相交可加。
- 失败与处理：整个 T08 唯一首轮失败 = part1 unit 单进程运行在 ~91% 进度被外部内存压力击杀（无 WER 事件、无 INTERNALERROR、无 JUnit；诊断为 TerminateProcess 家族，机器持续 88-89% 负载 + 外部进程活动）。处理：graph 按检查点规则复用（exit 0，log+junit sha 保全）、unit 拆 4 个不相交批重跑全绿；证据 evidence/first-attempt/fa-unit-run1.{log,meta.json}、fa-gate-part1.log（sha 5cc9b62f…/e4999e9d…）。启动脚本自身的 3 次 bug（未定义变量/模板字面量/venv launcher 双进程误判）隔离在 evidence/launch-bugs/，定性为启动器问题非 pytest 失败，含 PPID 审计更正记录。
- 无 skip/xfail/删测试/放宽断言制造绿色；integration 中 t05 期间观测到的 8 例 rg-PATH 失败经 A/B 证明（HEAD 原版 index.py 同样失败）为 shell PATH 环境问题，gate 注入 rg 后不复现，未改测试文件。
- gate summaries：test-gate-part2.json（sha 3855edf6…）、test-gate-part3.json（sha 3194ff0a…）、runs/t08-gate*.log；每组 log/junit 完整 sha256 均在 docs/notes/phase33-runtime-results.json groups 表内。

## 三、性能观测

单仓库（本仓，导出 2417 文件）、单次测量、机器负载 85-89%；数值均程序化读自 evidence/t05 两个 profile JSON，详见 docs/notes/phase33-index-profile.json。

| 场景 | Phase32 原始（任务书） | Phase33 基线复现 | 修复后 |
|---|---|---|---|
| cold | 68.43s | 74.37s | 103.89s |
| nochange | 3.07s | 2.98s | 7.32s |
| change | 82.51s | 86.32s | 45.00s（−47.9%） |
| change_body | 24.49s | 37.12s | 37.13s |
| （全量对照）verify_change | — | 79.59s | 73.85s |

- 解析/复用/边重建：parsed 965（两侧一致）、content_reused 2416/2417、最终 edge_rows 152371 / symbol_rows 10271 与基线完全一致；Phase32 的 151822 条边整图删插已被闭包有界重建取代（change 场景 delete 14.77→3.12s、insert 23.54→4.01s）。
- 验收：change <= cold 且 change_body <= cold 成立，快路径默认开启，无旁路。
- cold/nochange 修复后数值膨胀为扫描 I/O 噪声（scan 22.41→36.77s，代码路径未变；同轮全量 verify_change 73.85s ≈ 基线全量 79.59s 交叉证明无全量路径回归）。
- 局限（不外推）：单仓库单次测量；高扇入 hub 文件变更时闭包可逼近全图（退化为旧路径，不会更差）；delta 通道按设计过近似（sound 非 minimal）；解析缓存仅进程内 LRU，冷进程重解析未变更源；结果不适用于大型仓库外推。

## 四、候选与回执

- candidate：phase33-candidate-v6，status pending，schema_version 5，manifest-kind full，无 active 指针（active.json 不存在）。
- parent：phase31-candidate-v5 —— 未改动，candidate.json sha256 b97ca8787e936c2895c84291fe360333e43a798aff1176bafc158558306c8754，仍 pending。
- source_commit：d3171135511de95dfcdf54feab7c4ef636bd37d6（C1）
- diff_base：e56ee0bb246f8fc1f7c207531786fe62c38e730c（v5 的 source_commit，为 HEAD 祖先，已验证）
- 4 文件 sha256：
  - candidate.json d51f134831b80f071163b45231d2314e64a0d513677eb6897c8f5693eb6acf94
  - candidate.diff 3e8e649690d6c756d1763c44797cf8dde74f3b53e980c9fe0795f87e306793fb（git apply -R --check 通过）
  - snapshot.json cb403a1992ec0ed9f6249a06a2b813e269fcd68cfd8524225f3643e3d9e6b905
  - sources.json 6994e6715378ee653850c4b2b3336c44f18dc4786c6a77febc130a1a73b97283
  - candidate_tree_sha256 b9b7669388806b115248cb833a3f2b6cfad7b475c37af13f373555880e4dc515
- sources.json 对 C1 git blob 全量核验：122 sources = 103 逐字节一致 + 19 仅差归档器 CRLF→LF 归一（v5 同文件同行为，长期归档器约定，非漂移；证据 evidence/t09-sources-blob-check.json）。
- receipt：configs/harness/receipts/phase33-candidate-v6.json，sha256 c169c6c900ecd624780841b18b6c63892763baeb35299c4fdcc46f3909afd407；manifest_commit cabf5ef（historical-baseline.json 48 files，manifest sha256 881798abd7b52c11606fcf361283bfaae7317620f666463f9be0c9cfb9a3931f，drift=[]，仅新增 4 条 v6 记录）。
- witness：.codex-pytest-tmp/phase33/external-witness.txt（未跟踪、仓库外证据区）= c169c6c900ecd624780841b18b6c63892763baeb35299c4fdcc46f3909afd407。
- verify：phase28_archive_verify.py --all --ignore-raw-drift --receipt … --expect-receipt c169c6c9… → exit 0；versions_checked 12（new 5 / old 7）、new_failures 0、unexplained 0、unknown 0、historical_files_modified false、baseline manifest 48 files mismatch 0、witness=verified、historical_integrity=ok、new_archive_status=passed、receipt checks 23/23（含 receipt_commit_ancestor、receipt_candidate_files blob+disk 双核对、manifest blob 一致）。全量 JSON 报告 runs/verify-v6-full.json（sha256 7584ac1847b967ac9880fc1844a6d625a7da7731933cc3482c98091b1ed48d0c），T04 消费的键映射 envelope 存 .codex-pytest-tmp/phase33/verify-v6.json（注明派生关系，零捏造）。

## 五、提交

4 个本地提交，不 push、不 PR、不发布、不激活：

- C1 = d3171135511de95dfcdf54feab7c4ef636bd37d6 "fix: close phase33 provider, gate and index gaps"：恰好 16 文件（11 修改 + 5 新增：scripts/phase33_runtime_results.py、src/codepilot/sandbox/phases.py、tests/api/test_phase33_windows_transient_locks.py、tests/unit/llm/test_phase33_provider_real.py、tests/unit/test_phase33_test_gate.py），+3578/−373；仅 scripts/ src/ tests/。
- C2 = 8c6df9594ef3d96ee9839a7ebe8e8deb84d592da "chore: archive phase33-candidate-v6 (pending, parent v5)"：仅 4 个候选文件，+6174。
- C3 = cabf5ef512f14112fe00171ace83d1cc8e3869ad "docs: freeze phase33-candidate-v6 into the historical baseline manifest"：仅 configs/harness/candidates/historical-baseline.json。
- C4 = 本报告所在提交：configs/harness/receipts/phase33-candidate-v6.json + docs/notes/phase33-{final-report.md,runtime-results.json,static-checks.json,provider-matrix.json,index-profile.json,delivery-receipt.json} 共 7 文件。
- 未提交项：172 项未跟踪历史证据（a10-*.txt、logs/ 等）原样保留、未提交、未删除；.codex-pytest-tmp/phase33/ 全部产物不提交；不提交凭据/大型日志/无关文件。C1 前 tracked clean 已确认；git ls-files --others 对账 183 = 172 基线 + 4 基线杂项口径 + 7 phase33 新文件（docs 2 + code 5，code 已入 C1）。

## 六、未验证能力

- 真实 provider 流式/usage/计费：未验证。本轮无预算、无凭据、无用户授权，T01b 按规范保持 blocked；真实代码路径经 fake transport 测试证明可达且恰好调用 1 次，但从未与真实端点通信；真实计费/usage 字段解析未经线上验证。
- Docker 全量：未运行（Phase33 禁止官方 Harness 全量与 Docker 全量）；仅最小 e2e 通过（71.26s，阶段计时由 src/codepilot/sandbox/phases.py 落盘 docker-minimal-2.log；首轮超时根因 images.build(path=大目录)+DNS 污染 registry，修复为 fileobj+语法指令剥离，镜像预热 codepilot-sandbox-base:py3.12 dd6bf0841294，44 分钟构建证据 evidence/t02/）。遗留风险：src/codepilot/eval/swebench_runner.py:423 存在同模式 images.build(path=大目录)，本轮未修（超出 T02 范围，已记录）。
- 大型仓库：索引与回归结论仅基于本仓 2417 文件，未外推。
- 长时间稳定性：API 修复经 20×2 目标循环 + 5 轮全组验证，未做 24h 级长跑；Windows commit-limit 型 OOM 为环境级问题，已隔离（分批+阈值）未根除。

## 七、下一阶段条件

1. 真实 provider 验证轮：PHASE32_REAL_BUDGET_CNY>0（≤20）+ 对应凭据 + 用户显式授权后运行 phase32_provider_matrix.py --real；real-verified/real-failed 逐行入账，usage 与费用账本核对，cost_known 语义执行。
2. Docker 全量/官方 Harness 前先修复 swebench_runner.py:423 的 images.build(path=大目录) 同模式风险。
3. 索引结论进入大型仓库（≥2 万文件）复测后再承诺默认参数；hub 高扇入退化场景补基准。
4. Windows OOM 若复现：先采集 commit-limit 曲线与进程清单，再评估夹具/分批参数；gate 阈值（本轮 min_free_mb=1200/min_headroom=2500）是保护底线不是修复。
5. v6 激活走独立激活决策流程；Phase33 不激活、不 push。

## 验收清单（逐项）

- [x] provider runner 有真实代码路径；fake 测试证明调用 1 次；无凭据逐行 blocked（12 passed；3 行 blocked、missing 逐行）
- [x] 真实 provider：本轮无预算+凭据 → 明确未验证（§六），未写成通过
- [x] 最小 Docker e2e 通过（71.26s，阶段日志完整）
- [x] test gate 独立 --basetemp，不删除全局 pytest 根（删除逻辑物理移除 + sentinel 测试）
- [x] runtime JSON 可由 --check 复现（exit 0，sha f1c37a32…），无硬编码 0/旧 commit
- [x] 索引 change 不慢于 cold（45.00 <= 103.89），快路径默认开启，无需旁路
- [x] dump_index 与全量逐字节一致（3 对 all_equal + lead 独立复核）
- [x] API flake 根因修复（重试 + 活 stderr 绑定；ValueError 0 次）
- [x] Windows OOM 根因说明（commit-limit 耗尽）+ 隔离措施（分批 + 阈值，标注保护性质）
- [x] mypy baseline 全路径键；范围内 0 新增错误（static failed=[]，exit 0）
- [x] graph/unit/integration/api/archive 全跑；唯一首轮失败（外部击杀）有处理与证据
- [x] 静态检查通过；CRLF（唯一 EOF 问题已修）与凭据扫描（0 命中）有记录
- [x] v6 pending、parent v5、source/diff/snapshot/hash 一致
- [x] verify --all 带 witness exit 0；无 active；v5 未改
- [x] 4 个本地提交；不 push；不提交凭据/大型日志/无关文件
- [x] 真实模型请求 0、新增费用 0（provider-matrix real_requests=0 为证）

## 附：证据索引（.codex-pytest-tmp/phase33/，未跟踪）

- runs/：t08-gate.log、t08-gate-part2.log/.done、t08-gate-part3.log、t08.done、docker-minimal-{1,2}.log+.done、verify-v6-full.json、verify-v6.log、verify-v6-preC4-text.log、t10-runtime-check.log、api-*.log（T06 20 轮）
- evidence/first-attempt/：fa-unit-run1.{log,meta.json}、fa-gate-part1.log
- evidence/launch-bugs/：3 次启动 bug 隔离 + duplaunch 更正诊断（PPID 审计规则）
- evidence/t02/、t05/（REPORT.md + 双 profile + 6 DB）、t06/（api-flake-analysis.md + oom-analysis.md）、t07-static/lead-cleanup.md、t09-sources-blob-check.json、phase32-baseline-hashes.txt
- 汇总 JSON：test-gate-part2.json、test-gate-part3.json、static-checks.json、verify-v6.json、external-witness.txt

## Post-delivery correction (Phase34)

本节由 Phase34 追加；上方 Phase33 原始交付记录保留不改写，仅顶部结论已改为
CONDITIONAL PASS。逐项验收现状见 docs/notes/phase34-final-report.md。

### 1. P0：runtime --check 在 Phase33 最终 HEAD 失败

- 事实：JSON 冻结于 C3 `cabf5ef`；C4 `dba045d` 是 docs/receipt 提交。原脚本每次
  `--check` 都用 `git log base..HEAD` 重算并把结果与文件比较，且 `head_commit`
  取当前 HEAD，因此 C4 被算成一条 code commit、最终 HEAD 永远多一条 → exit 1。
- 复现（Phase34 起点 `dba045d`，日志 `.codex-pytest-tmp/phase34/pre-fix-check.log`）：
  `scripts/phase33_runtime_results.py --check` → exit 1，
  `sections=["code_commits","head_commit"]`。
- 修复（C5 `3a97b98`）：
  - 新增 `recorded_through`（证据记录到的提交）与 `phase_commits`（base..recorded_through）；
  - `code_commits` 改为按路径过滤（`src/ scripts/ tests/ web/src/ pyproject.toml
    web/package.json`），docs/archive/receipt 提交不再计入；
  - `--check` 从已写入 JSON 读取 `recorded_through`/`head_commit`（不读取当前 HEAD），
    验证其为 HEAD 祖先，且 `recorded_through..HEAD` 只允许 `docs/` 路径变化；
    重建 payload 时使用记录值。硬编码 base 已删除；
  - `--write` 在内容未变时保留旧 `generated_at`，连续两次写入字节一致。
- Phase33 证据已恢复为 C4 原样：`phase33-runtime-results.json` sha256
  `f1c37a32a7a29eb831ea172d36fd5b1c8aba5c934f2594d07fd9456467a23330`，
  `phase33-delivery-receipt.json` sha256
  `b0a97696d9a9f25f68a7f3aa67eceae85fb7e4697bc8d5d661dc2689e6df8acd`。
  Phase34 的生命周期验收改由 `docs/notes/phase34-runtime-results.json` 承担，
  其 `recorded_through` / `recorded_head` 语义与 C5 修复一致。
- Phase33 JSON 是冻结历史，不再用 `scripts/phase33_runtime_results.py --check`
  校验（恢复后没有 `recorded_through`，在该 HEAD 会报
  `recorded_through_missing`）；详见 `docs/notes/phase33-errata.md` 与
  `docs/notes/phase34-final-report.md`。
- 上方验收清单“runtime JSON 可由 --check 复现”（第 147 行）在 Phase33 交付时
  于 C4 不成立；Phase34 修复后由 Phase34 JSON 成立。因此本报告不再声称
  16 项全部闭环。

### 2. 性能口径修正（原始数值不变，撤回因果表述）

- “无全量回归”的表述撤回：单仓、单次测量不能支撑“无回归”的因果结论。
- 有本轮数据支持的结论：change 路径改善（86.32s → 45.00s，−47.9%）。
- cold（74.37 → 103.89s）与 nochange（2.98 → 7.32s）变慢，存在回退风险。
- scan 22.41 → 36.77s 是观察到的相关性，不是已证明的因果；同轮全量
  verify_change 73.85s vs 基线 79.59s 只是该轮对照，不能外推。
- 单仓（2417 文件）、单次测量、机器负载 85–89%，未重复测量，不外推。

### 3. 其它修正

- 归档 normalization 文案与实际不符（git_commit 模式声称存了 raw worktree
  hashes，实际未存）由 C6 修正：新候选带 `normalization_policy`，历史 v5/v6
  只标注 `normalization_note_stale=true`，不因此失败。
- OOM 继续定性为“隔离/保护”而非修复；gate 阈值 `min_free_mb=1200` /
  `min_commit_headroom_mb=2500` 在低内存机器上可能拒绝运行。
- 真实 provider、Docker 全量、大仓、24h 长稳仍未验证（见 Phase34 报告）。

