# Phase35 收尾报告：v13 归档、最终回归与外部验证

结论：**Phase34/v13 归档、九组回归与已执行的外部验证全部 PASS。** 仍未覆盖的是
real provider streaming、浏览器 E2E、完整历史 clone 和 P2 build-context guard；
这些是明确的剩余项，报告不把它们写成通过，也不把本报告等同于“所有生产条件均已就绪”。

- v13 归档：PASS（19 版本、12 new / 7 old、baseline 76、witness verified）。
- 九组全量回归：PASS（4182 passed / 0 failed / 4 skipped）。
- 静态检查：PASS（17 文件；仅生成物 diff 命中）。
- 前端：PASS（typecheck/lint/vitest 14 files / 50 tests/build）。
- 20k 大仓：PASS（20,201 文件，冷 208.4s、无变化 77.5s、单文件 73.5s、fast 72.6s）。
- Docker 全量：**PASS**（Bash/Git Bash 环境 35/35，316.44s）。
- 24h soak：**PASS**（1440 cycles、0 errors、0 lock/checkpoint failures、RSS 斜率
  -98,110 bytes/cycle）。
- 真实 provider：**PASS**（GLM 与 Spark 各 1 次 `real-verified`；Spark 无价表，
  `cost_known=false`）。
- 完整历史 clone：未验证（源仓库 shallow，边界不在 origin）。

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
  外部 witness 文件 sha256 同值。
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
`D:/cp35/final13`；此前 `D:/cp35/regression` 路径中的 “regression” 触发了
issue analyzer 的规则，导致两个 unit-a 失败，已用中性路径复跑并全绿。

静态：17 个变更 Python 文件；AST、ruff、format、mypy（changed src/scripts
0 new errors）、EOL、credential scan、web/src scope 通过；仅
`git diff --check` / CRLF-aware 命中既有生成物。sha256
`e05860e5d6e461e387a7b4eee1dfeaa9a9fab8205371d450410f16c3ac479be8`。

## 三、外部验证

### 前端（PASS）

`npm ci`、typecheck、lint、vitest **14 files / 50 tests**、build 全部通过。
仓库没有 Playwright/Cypress；浏览器 E2E 仍是已知缺口，报告不把它写成通过。
`frontend-summary.json` sha256 `97463c88b0eb61817ce06c5c7fc76785d84d9eba69b0e6bd7992282831791244`。

### 20k 大仓（PASS）

`phase32_index_profile.py`：20,201 文件；cold 208.4s、no-change 77.5s、
single-file 73.5s、fast 72.6s；40,200 symbols；0 parse errors；DB 24.5 MB；
峰值 working set 332–463 MB。原始 JSON 与 summary 在证据包
（`large-repo-summary.json` sha256 `a78d8587a4d40ed66e21b7e8e4395051dab4c3b5d746eb00dd563dda1b49b7e9`）。

### Docker 全量（PASS，35/35）

- 在 Bash/Git Bash 环境中 **35 passed / 0 failed / 0 skipped，316.44s**；权威
  JUnit 为 `docker-junit-bash.xml`（sha256
  `4c9919b75992d3933ba6a2e37eb3d586f77f771c82b2007dcdfc2dd376b78198`）。
- PowerShell Terminal 下曾有 26/35，9 个失败全部在
  `tests/docker/test_synthetic_suite.py`，错误为
  `codepilot.repo.workspace.WorkspaceError: base_commit 不在缓存中`；同一套件在
  Bash 下 35/35，说明是 PowerShell 终端环境的 git/workspace 差异，不是 v13 代码
  或 fixture 缺陷。失败的 PowerShell JUnit 保留为历史。
- `docker-summary.json` sha256
  `5e76c768b5b61da6c68d74cdebf2439184048e0b115833d09b934b99072d6c7c`。

### 24h soak（PASS）

- 运行区间：2026-11-05 18:54:09 UTC → 2026-11-06 18:54:09 UTC，完整 24h。
- **1440 cycles、0 errors、0 lock failures、0 checkpoint failures**；每 cycle
  都验证 graph 以 `PROVISIONALLY_VERIFIED` 结束、checkpoint 可读、workspace 锁
  可重新获取。
- RSS（十进制 MB）：首 140.9 / 末 145.5 / 峰值 695.4；锯齿波动，线性回归斜率
  **-98,110 bytes/cycle**，没有向上趋势，不构成泄漏证据。
- `soak-summary.json` sha256
  `4e6f1d01310f6a5c939bb54aca5c1ab5469572338d1b4586d0a114ba00661933`；
  完整心跳 `soak-heartbeat.jsonl` sha256
  `cbf4bf6c2564e169a3a2c7ce19d18e34c1c55eecf7c1ddc5aeddf3814ae63a1c`。
- 之前的失败尝试（全量回归/大仓/Docker 负载下的 `WorkspaceError`）与两次各 15
  cycles 的空闲 smoke 保留为历史；本轮最终运行在 Docker 停止的空闲环境完成。

### 真实 provider（PASS，两个模型）

- 协议：OpenAI-compatible；预算门 `PHASE32_REAL_BUDGET_CNY=20`；每个模型
  1 次请求、无重试、60s 超时。
- `glm-5.3-flash`（BigModel）：`real-verified`；17 input / 16 output tokens；
  cost `0.0000584 CNY`，`cost_known=true`。
- `spark-x2.6`（讯飞星辰 MaaS）：`real-verified`；25 input / 16 output tokens；
  未配置该模型价表，`cost_known=false`，`cost_cny=0.0` 仅是下界，不宣称免费。
- 合并矩阵：real requests **2**，42 input / 32 output tokens，`cost_known=false`
  （两次运行取 AND），`cost_unknown_calls=1`，`cost_is_lower_bound=true`；
  fake 166 tests 不重复求和。`provider-matrix.json` sha256
  `f77ff369a1b3b9ec48d88c6b42488a877a550910c0a35fa46f432a3c12925821`。
- 网络诊断：Spark 前两次尝试经本地代理 `127.0.0.1:7899` 时 TLS 握手失败
  （`real-failed/uncertain`）；直连 `maas-api.cn-huabei-1.xf-yun.com` 可达
  （TLS 0.186s，无 key 返回 HTTP 401）。用 `NO_PROXY='*'` 重跑后成功。两次
  uncertain 尝试与原始日志保留在 `provider-real/spark/`；GLM 与 Spark 的
  JUnit、日志、provider JSON 在 `provider-real/glm`、`provider-real/spark`。
- 凭据只存在于本机 gitignored `.env` / `.env.spark`，未进入证据包或提交；
  建议轮换曾在聊天中出现过的 Spark key。

## 四、统计口径

- `git rev-list --count HEAD`：C38 为 **318**；C47 为 **327**（C39–C47 共 9 个
  docs/代码提交）。
- 有效 SOURCE_SCOPE **787** 文件（含 `configs/harness/`，排除
  candidates/receipts）；v13 的 134 是“父候选继承 + scope 变更”的策展集合，
  覆盖 `cc0dd9a..C40` 的全部 scope 变更。
- 主 checkout untracked：`git status` **172** 条目 / `-uall` **176** 文件
  （`logs/` 折叠 5→1）。
- 详见 `docs/notes/phase35-errata.md`。

## 五、Shallow 历史、clean-clone 与 P2

- 源仓库 shallow；`.git/shallow=04a523f`，不在 origin 分支；`--unshallow`
  no-op。clean-clone 在 C47 复跑通过（`SOURCE_SHALLOW=true`、
  `CLONE_SHALLOW=false`、`SELF_CONTAINED_CHECK=PASS`），证明默认 `--check` 不
  依赖生成机 scratch；但源历史本身缺少 boundary 之前的部分，完整历史 clone
  仍未验证。日志 `docs/notes/phase35-selfcontained-check.log` sha256
  `df846448e0e42249e95229d5a26fd336e20150eb6cedfd9d14c6c0424ea67c31`。
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
| 8 | Docker 全量 | **通过（35/35，Bash）** | `docker-junit-bash.xml` |
| 9 | 24h soak | **通过（1440 cycles，0 errors）** | `soak-summary.json` |
| 10 | 真实 provider | **通过（GLM/Spark 均 real-verified；Spark cost 未知）** | `provider-matrix.json` |
| 11 | 默认 `--check` / 84 项证据清单 | 通过 | runtime JSON / SHA256SUMS |
| 12 | clean-clone | 通过（C47） | `phase35-selfcontained-check.log` |
| 13 | 未 push、未 PR、未激活 | 通过 | 无远端操作、无 active |
| 14 | streaming / 浏览器 E2E / 完整历史 / P2 | **未覆盖** | 见第七节 |

## 七、剩余项

1. **real provider streaming**：矩阵只覆盖非 streaming 请求；需要补 streaming
   chunk、usage 与 deadline 的真实网关测试。
2. **浏览器 E2E**：仓库没有 Playwright/Cypress；需要新增套件。
3. **完整历史 clone**：源仓库 shallow 且 boundary 不在 origin，当前无法补全。
4. **P2 build-context guard**：审计完成但未实现；若要修，需要改 `src/` 并生成
   新候选。
5. **Spark 费用**：未配置价表时 `cost_known=false`；若需要费用核算，应在
   `configs/llm.yaml` 增加价格并重跑（不得编造价格）。
6. **运行环境**：访问讯飞 MaaS 时需绕过本地 `127.0.0.1:7899` 代理（或把
   `xf-yun.com` 加入 `NO_PROXY`），否则会重复 TLS 失败。

## 附：证据索引

- 主证据包：`docs/notes/phase34-evidence/`，`SHA256SUMS` **84 项**
  （sha256 `ac70b3e5193936204e9ae88e70c7e29bb1a1c62ad67103b5e6a9d3e687a18de0`）。
- runtime JSON：`docs/notes/phase34-runtime-results.json`，sha256
  `cda4c935392e36ab45919fef41868e9a7893e8b51caa1aebbe75d7b4f998f423`。
- Phase35 证据：`verify-v13.json`、`large-repo-summary.json`、
  `frontend-summary.json`、`docker-summary.json`、`soak-summary.json`、
  `provider-matrix.json`、`provider-real/`、`phase35-selfcontained-check.log`。
- Phase34 统计勘误：`docs/notes/phase35-errata.md`。
- 生成时 scratch：`D:/cp35/`，不是交付依赖。
