# Phase29 运行时验证（T17，2026-10-31）

主控单智能体串行执行；零真实模型请求、零外部网络（全部 127.0.0.1 回环）、零费用、
零 Docker 运行（机器 daemon 可用 29.8.2，任务书禁用 → 全部测试命令 `-m "not docker"`）。
机器：Windows 11（10.0.22631）+ Git Bash，Python 3.12.7（`.venv`），pytest 9.1.1，
git 2.51.1.windows.1。执行日志统一在 `.codex-pytest-tmp/phase29/`（ledger.md 逐任务记录）。

## 1. 基线与提交链

- 起点 HEAD：`38e395a`（tracked 工作树零改动，未跟踪 173 项）。
- 提交链：`9ccc703` → `1e2c443` → `770c0dc` → `38e395a`（phase28）→ **`b17019b`（本轮源码提交，26 文件 +5554/−1698）** → docs/候选提交（见交付回执）。
- 源码提交后工作树 tracked 零改动（候选生成前提，实测 `git status --porcelain --untracked-files=no` 为 0 行）。

## 2. 归档链完整性（I1/I2/I3）

- schema v3（`docs/notes/phase29-archive-schema.md`）：`phase29-candidate-v3` 生成于源码提交之后，parent=`phase28-candidate-v2`，`source_origin=git_commit`，source_commit=`b17019b`，diff_base=`38e395a`，26 源文件全部从 commit blob 读取（LF 哈希与 `git show source_commit:<path>` 逐文件一致）。
- 校验器 19 项检查全过（`base_commit_blob`、`candidate_payload_sha256`、`diff_forward_apply`、`diff_reverse_apply`、`source_commit_ancestor`、`source_commit_blob`、`source_commit_resolves` 等）；`--all` 全链 9 候选（7 old + 2 new）exit 0。
- 历史 manifest：32 文件零漂移；28/32 文件与 `9ccc703` 基线 blob 逐字节一致（4/32 为 phase28-candidate-v2，晚于基线新增）。
- 旧 v1 生成路径：`rsi.archive_candidate` 硬失败 stub；rsi CLI 无 archive 子命令；phase26 测试改为直写 snapshot 夹具并断言 retired。
- 机器结果：`docs/notes/phase29-archive-integrity.json`。

## 3. 测试结果（T14 最终回归，代码冻结后）

| 组 | 命令 | 退出码 | 结果 | 日志 |
|---|---|---:|---|---|
| A | `pytest tests/unit/repo/ tests/unit/storage/ -q -m "not docker"` | 0 | 413 passed in 252.48s | `t14-final-a.log` |
| B1 | `pytest tests/integration/phase28/ -q -m "not docker"` | 0 | 7 passed in 35.16s | `t14-final-b1.log` |
| C+D | `pytest tests/unit/test_phase2{8,9}_archive*.py tests/unit/test_phase29_archive_security.py tests/unit/test_phase26_{harness,closeout}.py tests/unit/llm/ -q -m "not docker"` | 0 | 609 passed in 365.85s | `t14-final-cd.log` |
| B2 | `pytest tests/graph/ -q -m "not docker"` | 1 | 16 failed, 250 passed, 13 deselected(docker) | `t14-final-b2.log` |

- 去重口径：按命令分组，组内不重复计数；四组合计 passed 1029（A 413 + B1 7 + C+D 609 + B2 250）。
- B2 的 16 例失败**经基线对照实证为历史遗留**：stash 本轮全部改动后在干净基线复跑同批，抽样 3 例（覆盖 3 个失败文件族：`test_path_sandbox_error.py`、`test_phase9_resume.py`、`test_phase11_closure.py`）全部在基线同样失败。抽样外的 13 例未逐一基线复跑（方法限制，如实记录）。
- 零 skip、零 xfail；docker 标记用例按约束排除（deselected 计数记录在案）。
- 本轮新增测试：生成器 15 + 校验器 13 + 对抗矩阵 20 + 索引增量 4 + HTTP 7 = **59 例**，全部无 skip/xfail/超长 timeout/弱化断言。

## 4. 索引性能（I6/T09/T10）

| 场景（450 文件合成仓，2 预热+3 测量中位） | 优化前 | 优化后 | 变化 |
|---|---:|---:|---|
| cold | 9.0819s | 9.0568s | 持平（全量路径未动，预期） |
| sameproc_nochange（verified） | 1.3578s | 1.3804s | 持平（早退路径未动） |
| **sameproc_change（验收指标）** | **9.1021s** | **5.1318s** | **−43.6%** |
| newproc_change | 2.7908s | 1.5007s | −46.2% |
| cache_128 / 512 / unlimited | 4.556 / 3.4925 / 3.7265s | 1.374 / 1.2062 / 1.2123s | −69.8% / −65.4% / −67.5% |
| 趋势仓 2500 文件 sameproc_change | 66.3482s | 30.9732s | −53.3% |
| 趋势仓 2500 文件 cold（单次） | 91.4559s | 94.4214s | 噪声范围（单次，如实标注） |

- 优化内容：受限提取 + 受限替换 fast path（symbol id 与内容无关 ⇒ 集合不变 ⇒ 不变 source 的行与全量重建逐字段一致；semantics 指纹不一致、符号集变化、文件删除一律回退全路径）。
- 语义等价证明：全库 dump digest 优化前后逐字节一致（baseline `e04648d8…`、variant `0cf958ab…`；452 files / 3234 symbols / 9581 edges）。
- 过程缺陷与修复：fast path 初版缺 semantics 指纹门控，被既有回归 `test_phase26_cold_index_rebuilds_old_semantics` 抓住（legacy 指纹 + IMPORT 行被删场景误跳重建），修复后该组 40/40 绿。
- 边界：结论仅限本轮合成仓库，不外推大型真实仓库（趋势仓仅看方向）。

## 5. HTTP 发布矩阵（I6/T11）

19 行三态矩阵（`docs/notes/phase29-http-matrix.md`）：19 verified（本地回环），46 例三连跑全绿（exit 0/0/0，49.45s / 54.38s / 66.94s）。本轮新增错误路径行 16–19（500 确定性分类 / 非法 JSON / 429 可观测重试 / 连接拒绝保守分类）。
**unverified（Live Gate 未获授权，未执行）**：真实 provider 端到端、Bedrock deadline/取消、TLS/代理 CONNECT、池化取消。不因本地回环结果改写为"已验证"。

## 6. 页面端到端（T12）

触发条件判定：`web/src` 零改动、API 用户可见字段无变化 → 按任务书 §7.8 走无 UI 变更路径，未重跑浏览器 E2E。前端四项检查全过：typecheck / eslint（--max-warnings=0）/ vitest / build 均 exit 0（日志 `t12-*.log`）。

## 7. 清理（T13）

删除 0 文件；183 项未跟踪全分类（164 历史轮次证据 + 4 历史汇总 + `.playwright-mcp/` + `logs/` 保留，10 项本轮交付物随提交入库）；`.gitignore` 仅新增 `.playwright-mcp/`（可再生成，文件保留）。详见 `docs/notes/phase29-cleanup.md`。

## 8. 静态检查（T15）

AST（939 文件，2 个故意损坏夹具豁免）、ruff check（仓库全范围 0 违规）、ruff format（本轮触碰文件全过；6 个历史遗留文件不在范围）、mypy（8 个生产文件）、web tsc/eslint、`git diff --check` 干净。机器结果：`docs/notes/phase29-static-checks.json`。

## 9. 未验证/未执行项（逐条，不以"已完成"掩盖）

1. 真实 provider 请求（0 次）与费用（0 CNY）：Live Gate 无预算与授权，未执行。
2. Bedrock deadline/取消：未验证（仅证明执行路径不经过 http_lifecycle）。
3. TLS/代理 CONNECT：未验证（回环假代理为 HTTP）。
4. 池化取消：未验证（回环观测不等价真实套接字回收）。
5. 大型真实仓库性能：未测量（合成仓库结论不外推）。
6. B2 组 16 例历史失败的修复：非本轮范围，已定位定性（历史遗留，基线复现）。
7. ruff format 6 个历史遗留文件：不在本轮触碰范围，未重排。
8. candidate_payload_sha256 的外部锚定依赖 git 提交链与本回执，无数字签名。
