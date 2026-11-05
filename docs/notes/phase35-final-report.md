# Phase35 收尾报告：v13 归档、最终回归与外部验证

结论：**Phase34/v13 功能与归档一致性 PASS；外部验证部分通过，项目尚不能称为全部开发完、可上线。**

- v13 归档：PASS（19 版本、12 new / 7 old、baseline 76、witness verified）。
- 九组全量回归：PASS（4182 passed / 0 failed / 4 skipped）。
- 静态检查：PASS（17 文件；仅生成物 diff 命中）。
- 前端：PASS（typecheck/lint/vitest 14 files / 50 tests/build）。
- 20k 大仓：PASS（20,201 文件，冷 208s、无变化 77.5s、单文件 73.5s、fast 72.6s）。
- Docker 全量：**FAIL**（26/35；9 个 synthetic fixture 的 workspace cache 失败）。
- 24h soak：**RUNNING**（干净空闲环境；预期 2026-11-07 02:45 +08:00）。
- 真实 provider：**BLOCKED**（`.env` 未提供；0 请求、0 费用）。
- 完整历史 clone：未验证（shallow 边界不在 origin）。

## 一、v13 归档

- `phase34-candidate-v13`：pending、parent `phase34-candidate-v12`、
  schema 5、full、`source_commit=C40 7d5e75d`、`diff_base=C33 254b0a8`、
  134 个策展源文件（有效 SOURCE_SCOPE 787）。
- 新增内容：`tests/unit/test_phase35_activation_dryrun.py`、
  `scripts/phase34_runtime_results.py` 的外部验证段与动态 unverified。
- 四文件 sha256：
  - candidate.diff `1ab950921132bafca44db0f8540694a1080cc213a3fec3779dec4324be8075d5`
  - candidate.json `ab6b8a83cbe0160ca0ac5e61e44f452edf5ad13dcc28928a8e407225b6cde801`
  - snapshot.json `08daf2a7bd0deed4a1e170fcf25bbf329a5d1fa5c91bd7ea4284169ec900a57d`
  - sources.json `ab89014d8fa0323e08015e2d36b785569a02845bec368a3844d4ec9b26ae8b8d`
  - tree `f1236ed4e22e649092772aa3c178e25c37c1721bbfe7c5b8558db510d86ffd20`
- receipt `configs/harness/receipts/phase34-candidate-v13.json` sha256
  `105bacf33d2b07e9a3b76920c4270d479e4b3ed4f878367d1405b78005c36f47`；
  外部 witness 同值。
- verify：19 versions / new 12 / old 7 / `new_failures=0` / `unexplained=0` /
  `witness=verified` / `historical_integrity=ok` / baseline 76 / 0 mismatch /
  v13 32/32。JSON sha256 `845dc56adda3225d797a9f2c1a2a3135571ee0040e6600526c7c74ba71adc1a7`。
- v12 及更早候选保留为历史；无 active 指针。

## 二、最终全量回归与静态

| 组 | 结果 |
|---|---|
| unit-a | 1535 passed |
| unit-b | 1138 passed, 2 skipped |
| unit-c | 427 passed（含 v13 的 activation dry-run 与 external evidence 测试） |
| unit-d | 25 passed |
| eval | 482 passed, 1 skipped（Docker 不可用） |
| sandbox | 66 passed |
| integration | 126 passed |
| api | 117 passed, 1 skipped |
| graph | 266 passed |

合计 **4182 passed / 0 failed / 4 skipped**（selected 4186）。scratch 使用
`D:/cp35/final13`；此前 `D:/cp35/regression` 路径中的 "regression" 触发了
issue analyzer 的规则，导致两个 unit-a 失败，已用中性路径复跑并全绿。

静态：17 个变更 Python 文件；AST、ruff、format、mypy（changed src/scripts
0 new errors）、EOL、credential scan、web/src scope 通过；仅
`git diff --check` / CRLF-aware 命中既有生成物。sha256
`e05860e5d6e461e387a7b4eee1dfeaa9a9fab8205371d450410f16c3ac479be8`。

## 三、外部验证

### 前端（PASS）

`npm ci`、typecheck、lint、vitest **14 files / 50 tests**、build 全部通过。
仓库没有 Playwright/Cypress；浏览器 E2E 仍是已知缺口，报告不把它写成通过。

### 20k 大仓（PASS）

`phase32_index_profile.py`：20,201 文件；cold 208.4s、no-change 77.5s、
single-file 73.5s、fast 72.6s；40,200 symbols；0 parse errors；DB 24.5 MB；
峰值 working set 332–463 MB。原始 JSON 与 summary 在证据包。

### Docker 全量（FAIL，26/35）

- 26 个非 synthetic Docker 测试通过；9 个失败全部在
  `tests/docker/test_synthetic_suite.py`。
- 错误：`codepilot.repo.workspace.WorkspaceError: base_commit 不在缓存中`；
  evaluator 的 workspace cache 无法解析 fixture `metadata.base_commit`。
- 复现：完整套件、聚焦 recheck、空闲完整重跑都是同样的 9 个失败；不是
  负载问题，也不是 v13 代码回归。
- 修复需要改 `tests/` 或 `src/`，会触发新候选 v14；本轮未做，故 Docker
  保持 failed，不写成通过。JUnit/summary 在证据包
  （`docker-summary.json` sha256 `154c3c41…`）。

### 24h soak（RUNNING）

- 最终运行在 Docker 停止、无其他重活的空闲环境；`work-final`、
  `heartbeat-final.jsonl`、`summary-final.json`。
- 之前的失败尝试：全量回归/大仓/Docker 负载下多个 cycle 报同一个
  `WorkspaceError`；失败心跳保留为 `soak-attempt1/2/3`。
- 空闲 smoke 两次各 15 cycles、0 errors、0 lock/checkpoint failures，RSS
  约 141→160 MB；这是最终 24h 运行可用的依据。
- 预期完成时间 2026-11-07 02:45 +08:00；到时由 scheduled task
  `phase35-soak-check` 检查 summary，报告不会提前写成通过。

### 真实 provider（BLOCKED）

- 用户提供 `glm5.3flash` / `spark-2.6` 两个模型，OpenAI-compatible 协议，
  预算门 20 CNY；但 worktree `.env` 未出现，`OPENAI_API_KEY` /
  `OPENAI_BASE_URL` 未设置。
- 本轮真实请求 **0**、费用 **0**；provider matrix 仍是 fake 166 测试。
- 提供凭据后可分别以两个 model id 各跑一次（每家 ≤1 请求、无重试、60s），
  合并 real rows；未提供价表时 `cost_known=false`，只记录 tokens，不编造费用。

## 四、统计口径

- `git rev-list --count HEAD` 在 C38 为 **318**（之后 docs 提交会增加）。
- 有效 SOURCE_SCOPE **787** 文件（含 `configs/harness/`，排除
  candidates/receipts）；v13 的 134 是“父候选继承 + scope 变更”的策展集合，
  覆盖 `cc0dd9a..C40` 的全部 scope 变更。
- 主 checkout untracked：`git status` **172** 条目 / `-uall` **176** 文件
  （`logs/` 折叠 5→1）。
- 详见 `docs/notes/phase35-errata.md`。

## 五、Shallow 历史与 P2

- 仓库 shallow；`.git/shallow=04a523f`，不在 origin 分支；`--unshallow`
  no-op。clean-clone 只覆盖现有历史，完整历史 clone 未验证。
- P2 审计：`src/codepilot/sandbox/swebench_env.py:423` 的
  `DockerSdkBackend.build` 无真实调用方；官方路径不 build；通用 base 构建
  已用 `fileobj` 微上下文（`src/codepilot/sandbox/docker.py:354`）。未改
  `src/`；作为潜在误用 guard 留待后续。

## 六、验收清单

| # | 项 | 状态 | 证据 |
|---|---|---|---|
| 1 | v13 source = 最终代码 HEAD C40 | 通过 | candidate.json |
| 2 | v13 pending、parent v12、diff base C33、无 active | 通过 | candidate.json |
| 3 | v13 19 版本 verify、12 new/7 old、baseline 76 | 通过 | `verify-v13.json` |
| 4 | 九组 4182 passed / 0 failed / 4 skipped | 通过 | 证据包 gate summaries |
| 5 | 静态 17 文件，EOL/mypy/ruff 通过 | 通过 | `static-checks.json` |
| 6 | 前端 build/typecheck/lint/vitest | 通过（无 E2E 套件） | `frontend-summary.json` |
| 7 | 20k 大仓索引 | 通过 | `large-repo-summary.json` |
| 8 | Docker 全量 | **失败（26/35，fixture）** | `docker-summary.json` |
| 9 | 24h soak | **运行中** | `heartbeat-final.jsonl` / 完成后 summary |
| 10 | 真实 provider | **阻塞（无凭据）** | `provider-matrix.json`（fake） |
| 11 | 默认 `--check` / 73 项证据清单 | 通过 | runtime JSON / SHA256SUMS |
| 12 | clean-clone | 待最终 soak 后复跑 | `phase35-selfcontained-check.log` |
| 13 | 未 push、未 PR、未激活 | 通过 | 无远端操作、无 active |

## 七、下一步

1. **Docker synthetic fixture / workspace cache**：需要 v14 代码/测试修复；
   本轮已定位并保留复现证据，未在 v13 上掩盖。
2. **24h soak**：等待 `summary-final.json`；若失败，报告按实际结果更新，
   不写成通过。
3. **真实 provider**：提供 `.env` 后跑 glm5.3flash / spark-2.6 两次；
   未提供则保持 blocked。
4. **完整历史 clone**：需要可用的 shallow 边界来源；当前不可解。
5. **前端浏览器 E2E**：仓库无 Playwright/Cypress；需要新增套件（v14）。
6. **P2 guard**：可选，新增 src 变更时一并处理。

## 附：证据索引

- 主证据包：`docs/notes/phase34-evidence/`，`SHA256SUMS` 73 项
  （sha256 `dba3c0701b39f7899800f32dfaf31676ec5c75907166e366563127a6bf6960e5`）。
- runtime JSON：`docs/notes/phase34-runtime-results.json`，sha256
  `37b939c939dc47035efdca82d8c62c148767946922fbcb6edef5edc50cb078de`。
- Phase34 统计勘误：`docs/notes/phase35-errata.md`。
- Phase35 证据：`verify-v13.json`、`large-repo-summary.json`、
  `frontend-summary.json`、`docker-summary.json`、soak 心跳/尝试记录。
- 生成时 scratch：`D:/cp35/`，不是交付依赖。
