# Phase 1 实测记录

> 最新：R-23 已修复，两个真实仓库索引均成功。下方先保留历史失败记录，再给出本轮根因、计数和原始证据。Phase 1 整阶段仍不标完成。

> 数字来自真实运行，不预设、不美化。**跑成什么样就记什么**；拿不到的数字明说拿不到。

## R-23 真实中型仓库索引（2026-10-10，follow-up 步骤 3a）

**手段**：`scripts/index_real_repo.py` —— 先经 SOCKS 代理把仓库 `git clone --bare`（唯一联网一步），
再把**本地** bare 仓库交给 `WorkspaceManager.create(mode="fetch")`，对固定 base commit 跑一次
`Indexer().index(...)` 并计时。两个允许的目标都试了：

| 仓库 | 固定 base commit | 结果 |
|---|---|---|
| pallets/flask | `c12a5d874c5a014495eb2db8a73f40037bc813ac`（3.0.3） | **索引中途原生崩溃（SIGSEGV / 0xC0000005 access violation）**，拿不到 Files/Symbols/Edges/Tests |
| psf/requests | `0e322af87745eff34caffe4df68456ebc20d9068`（v2.32.3） | **同样原生崩溃于 `Indexer().index` 阶段**，无计数 |

**没有可报告的成功计数**——两个真实仓库都没能跑完一次索引。**不是耗时数字问题，是崩溃。**

### 崩溃定位（证据）

- 栈顶帧：`src/codepilot/repo/parser.py:288` → 即 tree-sitter Python 绑定的 `Node.children`
  被调用时，由 `repo/symbols.py:112 extract_symbols` 的迭代遍历触达。崩溃发生在**绑定/C 层**，
  我们的 wrapper 只是把 `self._node.children` 包一层。
- 逐文件隔离探针（`artifacts/followup-*/probe_flask_crash.py`，每文件独立子进程）：
  - flask：82 个 `.py` 里 **21 个** `extract_symbols` 崩溃（多为 `tests/test_*.py`）。
  - requests：36 个 `.py` 里 **10 个**崩溃，**含核心模块** `src/requests/{adapters,auth,cookies,models,sessions,utils}.py`
    与 `docs/conf.py`、`tests/test_requests.py` 等。触发文件都是普通真实代码，不怪癖。
- 对照：**合成 fixture（mini_auth / mini_calc / mini_pkg）与全部 746 条单测/集成测试均正常**，
  golden 索引 58 symbols / 203 edges 稳定。崩溃只在**真实仓库规模的树**上出现。

### 旧根因假设（已否定，2026-10-10 修订）

上一轮提出“包装层未保存 Tree 导致 Node 悬空”，但没有验证。该推断不成立：
tree-sitter 0.26.0 的 `node_new_internal` 使用 `Py_NewRef(tree)`，Node 自身持有 Tree 强引用。
本轮原生 API 复现始终保留 Tree，仍可崩溃，因此没有向 ParsedFile 或包装节点添加冗余 Tree 引用。
旧日志不完整，无法补造历史终端输出；本轮在相同固定提交重新采集完整证据，见下方修复记录。

### 当时结论（历史记录，最新状态见下方）

- **R-23 目标未达成**：Phase 1 索引管线**尚不能对真实中型仓库跑出结果**，这是被 R-23 暴露出的
  **Phase 1 健壮性缺口**，不是环境/网络问题（联网 clone 与本地 fetch workspace 都成功，崩在解析阶段）。
- 原先建议添加 Tree 强引用已撤销；实际修复必须依据独立子进程失败测试和依赖版本对照。
- 本项在修复前保持为**已知缺口**，不记为通过。

<!-- R23:748219c2068323dc68fca2e39e9b7abd06b457a076c421993929cc1a6daa70a8:start -->
- 仓库：`https://github.com/pallets/flask.git`
- base commit：`c12a5d874c5a014495eb2db8a73f40037bc813ac`
- 模式：fetch workspace（自包含、只含 base 祖先）
- 索引耗时：**19.31s**
- Files：**241**；Symbols：**1644** （class=156, function=665, method=357, module=82, test_function=355, test_method=29）
- Edges：**10839**（CALL=3434, IMPORT=642, INHERIT=71, REFERENCE=428, TEST=6264）
- Tests：**384**
- 解析错误文件：0；扫描跳过：{'files': 241, 'ignored': 0, 'too_large': 0, 'binary': 8, 'symlinks': 0, 'unreadable': 0}
<!-- R23:748219c2068323dc68fca2e39e9b7abd06b457a076c421993929cc1a6daa70a8:end -->

<!-- R23:46a9808ba19b14a103395995cbeae949cbb13bfab4652068b7d10f78a2e72c28:start -->
- 仓库：`https://github.com/psf/requests.git`
- base commit：`0e322af87745eff34caffe4df68456ebc20d9068`
- 模式：fetch workspace（自包含、只含 base 祖先）
- 索引耗时：**9.81s**
- Files：**120**；Symbols：**789** （class=86, function=121, method=213, module=36, test_function=53, test_method=280）
- Edges：**7059**（CALL=2498, IMPORT=471, INHERIT=36, REFERENCE=159, TEST=3895）
- Tests：**333**
- 解析错误文件：0；扫描跳过：{'files': 120, 'ignored': 0, 'too_large': 1, 'binary': 10, 'symlinks': 0, 'unreadable': 0}
<!-- R23:46a9808ba19b14a103395995cbeae949cbb13bfab4652068b7d10f78a2e72c28:end -->

## R-23 修复验收（2026-10-10）

**R-23 已完成：两个固定提交的真实索引均完整成功，退出码均为 0。Phase 1 整阶段不在此标为完成；本次 Windows 验收时 Linux CI 尚未运行，Phase 2 未启动。后续 Linux/Docker 验收见下节。**

| 仓库 | Files | Symbols | Edges | Tests | 索引耗时 | 解析错误文件 | 扫描跳过 | 已解析 Python 文件 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| flask | 241 | 1644 | 10839 | 384 | 19.31s | 0 | 8 | 82 |
| requests | 120 | 789 | 7059 | 333 | 9.81s | 0 | 11 | 36 |

扫描跳过沿用既有规则：Flask 为 8 个二进制文件；Requests 为 10 个二进制文件和 1 个超过 1MB 的文件。
七项顺序门禁退出码全部为 0；全量离线测试 **752 passed、0 skipped、0 warnings、覆盖率 94.21%**，比原有 746 项新增 6 项。
fixture CLI 为 14 Files / 58 Symbols / 200 持久化 Edges / 7 Tests；golden 继续匹配 58 Symbols / 203 原始 Edges（既有持久化去重语义）。
测试结束后的六轮交替性能对照：mini_auth 解析与 symbol 提取中位数的中位数，旧版 **5.507ms**、锁定版本 **5.469ms**，
未观察到退化；完整 100 次样本/轮与顺序见 `performance-comparison.json`。

- Flask 3.0.3：`c12a5d874c5a014495eb2db8a73f40037bc813ac`。
- Requests 2.32.3：`0e322af87745eff34caffe4df68456ebc20d9068`。
- Files 包括可扫描的非 Python 文件；Symbols/Edges/Tests 是索引结果计数，并非执行这些仓库的测试套件。
- 原始扫描分类、逐文件解析路径与完整精度耗时见 `index-flask/result.json`、`index-requests/result.json`。
  全部 82/36 个 Python 文件均实际解析；没有为通过验收跳过触发崩溃的文件。

### 根因与对照证据

环境：Python 3.12.7（Anaconda，MSC 1929，AMD64），Windows 11 `10.0.22631`。
失败组合为 tree-sitter **0.26.0** + tree-sitter-python **0.25.0**；修复组合精确锁定为 **0.25.2 + 0.25.0**，`uv.lock` 同步更新。

根因是 0.26.0 的 `Point.row` / `Point.column` getter 把 `PyTuple_GetItem` 的**借用引用**直接返回，
没有增加 Python 返回值所需的引用计数。取出坐标时引用数实测保持 `3 → 3`，正确行为应为 `3 → 4`。
对于超出 CPython 小整数缓存范围的坐标（最小用例取 257），临时 Point 或属性值的释放可过早释放整数，
造成内存损坏；后续崩溃帧可出现在 `Node.children`、`text` 或 GC。因此不能把最后崩溃帧当作最初的错误操作。
上游 [0.26.0 point.c](https://github.com/tree-sitter/py-tree-sitter/blob/v0.26.0/tree_sitter/binding/point.c)
与 [node.c](https://github.com/tree-sitter/py-tree-sitter/blob/v0.26.0/tree_sitter/binding/node.c)
的源码快照已保存到 artifact；后者的 `Py_NewRef(tree)` 也否定了“ParsedFile 必须额外持有 Tree”的假设。

验证不是依赖猜测：

1. 使用 Requests `src/requests/models.py` 和 Flask `src/flask/app.py`，每个文件、每个模式各 3 个独立进程。
   旧版本 `extract_symbols` 与原生坐标属性遍历各 **3/3 崩溃**，退出 `3221225477 / 0xC0000005`；
   同样的遍历把坐标属性改为元组下标后各 **3/3 成功**。原生模式全程保留 Tree，未使用项目包装层。
2. Requests 的原生 `children` 遍历及 `field/text` 对照均完成，定位到坐标属性接口而非遍历策略或业务源码语法。
3. 最小源码为 `b"\n" * 257 + b"def f():\n    return 1\n"`，重复提取 100 次。
   旧版本 **3/3 原生崩溃**，0.25.2 **3/3 成功**；row/column 的引用计数断言在旧版各 3/3 失败、新版各 3/3 成功。
4. 完整逐文件探针：旧版 Flask **21/82**、Requests **9/36** 崩溃；修复后分别 **0/82**、**0/36**。
   历史报告称 Requests 10/36，但旧日志缺失，不能还原；9/36 是本轮实际观测，不改写为历史数字。
5. 新增 4 项子进程测试覆盖 row、column、extract_symbols 和 Tree/ParsedFile/Parser 释放后 `children`、`field()`、`text` 生命周期；
   另新增 2 项实测记录幂等测试，共 **6 项**。生产 parser/节点包装层/遍历策略均未修改。

### 验证与证据入口

全部产物保留于 [`artifacts/r23-repair-20261010/`](../../artifacts/r23-repair-20261010/README.md)：

- `flask-before.jsonl` / `requests-before.jsonl`：每个文件独立进程的完整 stdout、faulthandler stderr、退出码、版本、源码 SHA-256。
- `api-isolation.json` / `repeat-before.json` / `minimal-version-matrix.json`：API 隔离、重复复现、最小化版本对照。
- `flask-after.jsonl` / `requests-after.jsonl`：同样全部文件修复后的完整探针。
- `target-before.log`：旧版真实失败测试（3 failed / 1 passed）；`target-unsandboxed.log`：修复后 6 passed。
- `gate1-*` 至 `gate7-*`：严格按 ruff check → ruff format --check → mypy src → 全量离线 pytest 覆盖率 → fixture CLI → Flask → Requests 顺序运行。
  每步 `.log` 是原始终端合并输出，`.json` 保存命令、工作目录、退出码、耗时和环境版本。
- fixture CLI 使用源目录的同内容隔离副本，实际提交 `a1f1dd7b04161d2d8be73e2fb979a86d2a8a35de`；
  原始 fixture 没有自己的 `.git`，不能把主项目 HEAD 当作 fixture commit。副本内容哈希见 `fixture.json`。
- 本次 Windows R-23 验收结束时有 **5 个未 push commit**：`208e668`、`b057e55`、`3ecd0a6`、`83c0d42`、`65e4c25`；
  本轮修改保留在工作树，未新建 commit、未 push、未创建 PR、未更改仓库可见性、未删除既有 artifact。

`write_notes()` 现在用 repo + commit 的 SHA-256 标记替换对应结果段落，其他仓库/提交和历史崩溃说明不受影响。
`--local-repo` 可复用已经取得的仓库，仍走 bare clone → fetch workspace → 完整 Indexer；`--repo` 保留原始仓库 URL。

LLM smoke 已运行且成功（deepseek-chat 1/1），不等同于 `@pytest.mark.llm` 测试；这些标记用例不存在。
本次 Windows R-23 验收结束时，Docker 无 docker 标记用例，Linux CI 因未 push 为 **NOT RUN**；这是当时状态，后续验证单独记录。
测试沙箱内两次临时目录 `WinError 5` 已保留失败日志；沙箱外同样测试通过，未为此添加业务重试或改动测试语义。

## Linux / Docker 验收补充（2026-10-10）

用户已授权提交、推送独立分支并创建 PR；仍只处理 Phase 1，不启动 Phase 2。
新增 1 项 `@pytest.mark.docker` 的容器索引验收与独立 CI job；daemon 不可用或用例全部跳过不能计为通过。
本机最终版本已真实执行：外层 1 passed / 0 skipped（60.66s）；Linux 容器内 11 项回归全部成功，
额外 CLI + SQLite 核对 14 Files / 58 Symbols / 200 Edges / 7 Tests / 0 解析错误。
原始日志、版本、退出码、JUnit 与构建输入哈希见 [Docker 本机证据](../evidence/docker-20261010/README.md)。
**Linux CI 与 Docker 均已实际运行并通过。** 修复提交为 `16dfa9b184ee7489617eddfce44a143b44ac432c`：
[Ubuntu push 运行](https://github.com/wanjiang263/CodePilot-Repair/actions/runs/38037605606) 与
[PR 运行](https://github.com/wanjiang263/CodePilot-Repair/actions/runs/38037648144) 的三个 job 均 success。

- Linux：Python 3.12.14、tree-sitter 0.25.2 / grammar 0.25.0；ruff check、format、mypy 全部成功。
- 离线测试：**747 passed / 5 skipped / 1 deselected，覆盖率 93.77%**（31.04s）；5 个 skip 全为 Windows taskkill/junction 专属测试。
- CI Docker daemon 28.0.4：**1 passed / 0 skipped / 752 deselected**（28.32s）；容器内 Python 3.12.7，11 项既有回归全部成功。
  额外 CLI + SQLite 得到 14 Files / 58 Symbols / 200 Edges / 7 Tests / 0 解析错误。
  容器 fixture 的实际 commit 为 `70fff789a6edbce33e87fda34475661867aa36bb`，在日志中回显。
- CI 的 daemon 检查及 JUnit 非空、零跳过门禁均通过；全部原始输出、环境、JUnit、任务结论与 SHA-256
  见 [Ubuntu CI 证据](../evidence/linux-ci-20261010/README.md)，GitHub 运行页亦附三个可下载 artifact。

[PR #1](https://github.com/wanjiang263/CodePilot-Repair/pull/1) 已创建，分支为 `codex/phase1-closeout-ci-docker`。
此前本地已有 **5 个 commit** 已随分支推送，本轮另提交 R-23 修复、7 项新增回归（6 项解析/幂等 + 1 项 Docker）与证据；
后续文档证据提交的运行结论以 PR 最新 checks 为准，以上链接对应明确的已验证实现提交。
仓库保持私有，未合并 PR，原 artifact 全部保留。Phase 2 未启动；Phase 4 产品沙箱未实现，
本次 Docker 测试仅验收 Phase 1 索引管线。Phase 1 整阶段仍不在本记录中标为完成。

R-23 的精选原始证据另保存在可提交的 [`docs/evidence/r23-20261010/`](../evidence/r23-20261010/README.md)，
原 `artifacts/r23-repair-20261010/` 全部保留。
