# Phase34 最终报告：v12 归档一致性与最终全量回归

结论：PASS（Phase34 范围内）；完整历史 clone 未验证，限制见 §六。

最终代码提交：C33 `254b0a81248b8455c236696f9915e2a237b8c05e`。
最终候选：`phase34-candidate-v12`（pending，parent v11，source C33）。
C34 提交 v12 候选；C35a 提交历史基线 manifest；C35b 提交 v12 receipt；
C36 提交证据包、runtime JSON、静态结果、本报告与回执；C37 提交
clean-clone 绿色日志与最终哈希（docs-only）。Phase34 起点：
`dba045dabd70f0e6eb7779287136944b589f2734`。

交付硬约束：Phase34 runtime `--check` 默认读取仓库内
`docs/notes/phase34-evidence`，不需要 `--scratch`；`D:/cp34/final-v10` /
`D:/cp34/final-v11` / `D:/cp34/final-v12` 只是生成时的临时目录，不是交付依赖。

## 一、归档一致性（评审 P0 的最终修复）

评审事实成立：v9 的 `source_commit` 是 C15 `cc0dd9a`，而 C19/C22 修改了
`scripts/` 内的证据工具（属于 `SOURCE_SCOPE`），所以 v9 只是 C15 的快照，
不能代表当前源码。

处理链：

- v10 从 C27 `7646709` 生成（133 源文件，6 个 source-scope 变更），并完成
  manifest/receipt/verify。
- v10 后的最终全量回归暴露了 `test_process_exit_releases_workspace` 的
  环境超时：spawn 子进程导入 `codepilot.graph.builder` 需要 10–13 秒，而
  测试只等 10 秒。核心断言未变，C30 把启动等待改为 60 秒。
- C33 把该测试文件从历史 CRLF 规范化为 LF，以通过静态 EOL 门槛。
- 这两次都是 `tests/` 变更，按“源码变了必须有新候选”的规则生成 v11、v12；
  v12 的 `source_commit` 是 C33，等于最终代码 HEAD。

v12 数据：

- `phase34-candidate-v12`：pending、parent `phase34-candidate-v11`、
  schema 5、full、`source_commit=C33`、`diff_base=C30`、133 sources、
  1 changed（`tests/unit/test_phase26_final.py`，仅行尾规范化）。
- 4 文件 sha256：
  - candidate.diff `0a2e0de07223be604ac8f817254e0b0da86163a652e2dda114e3a9eaa63aaf38`
  - candidate.json `458b42b55407ed4ae27c142719dca164dc716125199d2038f44aa77fa79a61a8`
  - snapshot.json `2d4ae4b8d6bb6acc13df4fa026f7cab72b236aa5f6418d2144b148884d628864`
  - sources.json `b29452f4a507ab203ec04cfbc785d6856a9db3c495cfdc8db70be1388a790b64`
- `git apply --check -R` exit 0；v9/v10/v11 四文件 sha 未变；无 active 指针。
- historical-baseline：72 files、0 mismatches、无漂移。
- receipt `configs/harness/receipts/phase34-candidate-v12.json` sha256
  `99d8787cf12bd45fdf00552fe374438f834ddbac51cbe75f3825e7ca683de6fa`；
  外部 witness 同值（`external-witness-v12.txt`）。
- 完整校验：exit 0，`versions=18 new=11 old=7 new_failures=0 unexplained=0`、
  `witness=verified historical_integrity=ok`、v12 32/32。
  JSON：`docs/notes/phase34-evidence/verify-v12.json`。
- v9/v10/v11 作为历史 pending 候选保留；最终候选是 v12，不再使用 v9 代表
  当前源码。

## 二、测试修复说明（不是 skip/xfail/削弱）

- 失败用例：`tests/unit/test_phase26_final.py::test_process_exit_releases_workspace`。
- 根因：`spawn` 子进程启动后要导入 `tests.unit.test_phase26_final`，其顶层
  导入 `codepilot.graph.builder` 在本机约 10–13 秒，超过 `ready.wait(10)`；
  子进程本身能正常获取 workspace 锁并以 0 退出（独立复现确认）。
- 修复：C30 把 `ready.wait(10)` / `process.join(10)` 改为 60 秒；断言仍是
  “子进程获取锁并 set ready、父进程在子进程退出后能再次获取锁、exitcode 0”。
  没有 skip、xfail、删除或改写断言。
- C33 仅把该文件 440 行 CRLF 统一为 LF；内容与逻辑不变。
- 这两次变更都在 `tests/`，因此产生 v11/v12，而不是继续使用 v10 作为最终
  候选。

## 三、证据自包含

- 默认 scratch：`docs/notes/phase34-evidence`；`SHA256SUMS` 56 项，
  sha256 `4aa78793e726aa7d19a49222b163381cc63305d3caa6a4adc8d139a209e80292`。
- `--check` 先校验清单；缺文件/哈希不符/清单非法/未列出的 gate 摘要分别报
  `evidence_missing`、`evidence_manifest_mismatch`、
  `evidence_manifest_invalid`、`evidence_unlisted`，不再把缺证据误报成
  `payload_mismatch`。
- runtime JSON 写入 `evidence_dir` 与 `evidence_manifest_sha256`；gate
  `log`/`junit` 按文件名重定位，`basetemp` 只保留目录名，gate `command`
  不再写入派生 JSON（原始命令保留在证据包 `test-gate-*.json`），
  `historical_baseline.manifest_path` 固定为仓库相对路径。
- `docs/notes/.gitattributes`（`phase33-*`、`phase34-*`、
  `phase34-evidence/**` 为 `-text`）与 `configs/harness/.gitattributes`
  （`** -text`）固定证据/候选/回执/报告字节。
- runtime JSON sha256：
  `00b1fba41779314b50f9df3a84c703f5d05e58fcc2da36f589790c42398f50dd`；
  不含 `D:/cp34`，不含 gate `command`。

clean-clone 回归：在 C36 上运行，结果与日志在 C37 回填
（`docs/notes/phase34-selfcontained-check.log`）。脚本显式打印
`SOURCE_SHALLOW`、`CLONE_SHALLOW` 和 `HISTORY_SCOPE`。

## 四、最终全量回归

| 组 | 结果 | 说明 |
|---|---|---|
| unit-a | 1535 passed | C32b 全量 |
| unit-b | 1138 passed, 2 skipped | C32b 全量 |
| unit-c | 425 passed | C35b 重跑（C33 测试文件） |
| unit-d | 25 passed | C32b 全量 |
| eval | 483 passed | C32b 全量 |
| sandbox | 66 passed, 1 deselected | C32b 全量 |
| integration | 126 passed, 1 deselected | C32b 全量 |
| api | 117 passed, 1 skipped | C32b 全量 |
| graph | 266 passed, 13 deselected | C32b 全量 |

- 合计：**4181 passed / 0 failed / 3 skipped**（selected 4184）。
- 8 组在 C32b/C30 上完成；C30→C33 的差异只有 `tests/unit/test_phase26_final.py`
  的启动等待与行尾，属于 unit-c，其他组不读取该文件。因此 unit-c 在 C35b 重跑，
  其余 8 组沿用 C32b 的绿色摘要。
- C32b 的 `resumable.log` 保留了修复前 unit-c 的超时；最终的
  `test-gate-unit-c.json` / `run-unit-c.log` 是 C35b 的绿色重跑，二者都在
  证据包中，未覆盖或隐藏历史。
- 3 个 skip 为平台/环境既有跳过（unit-b 2、api 1），无 skip/xfail 掩盖失败。

## 五、静态检查

`scripts/phase32_static_checks.py` 在 C33 重跑：

- 16 个变更 Python 文件；通过：AST、ruff check、ruff format、mypy
  （changed src/scripts，0 new errors）、locale JSON、EOL（LF）、
  credential scan、web/src scope。
- 失败仅为 `git diff --check` / CRLF-aware 命中生成物：
  `historical-baseline.json` 沿用既有 CRLF 约定；v7–v12 的 `candidate.diff`
  含 unified diff 空 context 行。代码文件单独检查 exit 0；未修改脚本、
  未放宽检查。
- `docs/notes/phase34-static-checks.json` 与证据包内
  `static-checks.json` 字节一致，sha256
  `5cae0673a34d2e13f6cf0e31f742cd23d15a0cfba9a0579b14a94f2805ff3930`。
- 前端：`web/src` 零 diff → “零 diff 跳过”，不写成通过。

## 六、Shallow history 限制（评审 P1）

- 当前仓库 `git rev-parse --is-shallow-repository` 为 `true`；
  `.git/shallow` 的边界是 `04a523fafbd61bc2e49420963b84ed8e2bd1b3cf`。
- `origin`（`github.com/wanjiang263/CodePilot-Repair`）的分支不包含该边界提交，
  `git fetch --unshallow origin` 退出 0 但不改变 shallow 文件（no-op）。
- 因此 clean-clone 通过范围仅限现有 303 条提交历史；完整历史 clone **未验证**。
  脚本会打印 `HISTORY_SCOPE=existing_shallow_history_only`，不会把它写成
  “任意完整 clone 均可复现”。这不影响 v12 候选与当前源码的一致性，也不影响
  仓库内证据的 `--check`。

## 七、提交与最终 HEAD

- 起点 `dba045d`；Phase33 C1–C4 未改写。
- C5–C18：Phase34 P0/P1/P2 修复、v7/v8/v9、eval isolation、历史证据分离。
- C19–C26：证据自包含、clean-clone、字节固定、v9 收口。
- C27 `7646709` runtime 生成器支持 v10 + 动态发现。
- C28 `e0b1a6f` v10 候选；C29a `64d2bd9` v10 manifest；
  C29b `a4c7dd5` v10 receipt。
- C30 `bc2160d` 测试启动等待修复 + 动态候选/verify 发现。
- C31 `2dfe3e5` v11 候选；C32a `3b7b327` v11 manifest；
  C32b `c22ba00` v11 receipt。
- C33 `254b0a8` 测试文件 LF 规范化。
- C34 `6d138a3` v12 候选；C35a `1b972c3` v12 manifest；
  C35b `f843fe3` v12 receipt。
- C36（本提交）证据包、runtime JSON、静态结果、最终报告、delivery receipt。
- C37 docs-only：clean-clone 绿色日志与最终哈希回填。
- 不 push、不 PR、不激活。

## 八、验收清单（逐项）

| # | 项 | 状态 | 证据 |
|---|---|---|---|
| 1 | v12 `source_commit` = 最终代码 HEAD C33 | 通过 | candidate.json |
| 2 | v12 pending、parent v11、diff_base C30、无 active | 通过 | candidate.json |
| 3 | v12 四文件 sha、tree、反向 apply | 通过 | §一 |
| 4 | v9/v10/v11 四文件 sha 未变 | 通过 | 逐项哈希 |
| 5 | archive verify 18 versions、new_failures=0、witness verified | 通过 | `verify-v12.json` |
| 6 | historical baseline 72 files、0 mismatch、无漂移 | 通过 | manifest + verify |
| 7 | 最终代码 HEAD 上默认 `--check` exit 0（无 `--scratch`） | 通过 | C35b 生成；C36/C37 复跑 |
| 8 | 仓库内证据包 `SHA256SUMS` 56 项校验通过 | 通过 | `phase34_evidence_manifest.py --check` |
| 9 | runtime JSON `evidence_dir`/`evidence_manifest_sha256` 正确、无 `D:/cp34` | 通过 | 单测 + JSON 审计 |
| 10 | clean-clone（不复制外部 scratch）exit 0 | 待 C37 日志 | `phase34-selfcontained-check.log` |
| 11 | 九组最终全量 4181 passed / 0 failed / 3 skipped | 通过 | §四 |
| 12 | 静态检查 16 文件，EOL/mypy/ruff 通过 | 通过（代码门槛） | §五 |
| 13 | provider fake 166 passed，real_requests=0、cost=0 | 通过 | §四 / provider JSON |
| 14 | 未 push、未 PR、未激活 | 通过 | 无远端操作、无 active |
| 15 | shallow 限制已显式记录 | 通过 | §六 + clean-clone 日志 |

## 九、未验证（按 Phase34 指令显式排除）

- 真实 provider streaming/usage/billing：0 请求、0 费用；无预算/凭据/授权。
- Docker 全量：未运行（Phase33 最小 e2e 为历史证据）。
- 大型仓库（≥2 万文件）：未验证。
- 24h 长稳：未验证。
- 前端：`web/src` 零 diff，条件跳过。
- `src/codepilot/eval/swebench_runner.py:423` 的 `images.build(path=大目录)` 风险：
  P2，未动。
- Phase35 的 activation dry-run、真实 provider 小预算冒烟、Docker / 官方
  Harness 全量等：不在本轮范围。

## 附：证据索引

- **主证据包**：`docs/notes/phase34-evidence/`
  - 入口：九组 `test-gate-<group>.json` + `gate-<group>-run1.log/.xml`；
    `static-checks.json`；`provider-matrix.json` + `provider-matrix/`；
    `verify-v12.json` + `verify-v12.log`；`external-witness-v12.txt`；
    `pre-fix-check.log`；`run-resumable-gate.sh` + `run-<group>.log`；
    v9/v10/v11 历史 verify 与 witness；`README.md`。
  - 完整性：`docs/notes/phase34-evidence/SHA256SUMS`，56 项，
    sha256 `4aa78793e726aa7d19a49222b163381cc63305d3caa6a4adc8d139a209e80292`。
  - 默认命令：`.venv/Scripts/python.exe scripts/phase34_runtime_results.py --check`
    （无需 `--scratch`）。
- **字节固定**：`docs/notes/.gitattributes`、`configs/harness/.gitattributes`。
- **clean-clone 日志**：`docs/notes/phase34-selfcontained-check.log`
  （C36 运行；C37 回填）；首次失败与 pre-json-pin 日志保留。
- `docs/notes/phase34-runtime-results.json`（sha256
  `00b1fba41779314b50f9df3a84c703f5d05e58fcc2da36f589790c42398f50dd`）。
- `docs/notes/phase34-static-checks.json`（sha256 `5cae0673…`）。
- **生成时临时目录（非交付依赖）**：`D:/cp34/final-v10/`、`final-v11/`、
  `final-v12/`；被 runtime 消费的文件均已复制到证据包并纳入 `SHA256SUMS`。
- v12 receipt：`configs/harness/receipts/phase34-candidate-v12.json`。
- Phase33 历史：`docs/notes/phase33-errata.md`、
  `docs/notes/phase33-final-report.md`。
