# Phase 1 实测记录

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

### 根因（假设，未证实）

最可能是 **tree-sitter 的 `Node` 与其底层 `Tree` 生命周期**：`SyntaxNode` 包装只持 `Node`（树内视图），
在遍历大树、中间 child 列表被 Python GC 回收后，再访问残留 `Node` 触发 use-after-free → access violation。
小 fixture 的树不够大/不够深，走不到这条释放路径，所以不复现。**此为假设，未用 ASan/最小化复现证实。**

### 结论与后续

- **R-23 目标未达成**：Phase 1 索引管线**尚不能对真实中型仓库跑出结果**，这是被 R-23 暴露出的
  **Phase 1 健壮性缺口**，不是环境/网络问题（联网 clone 与本地 fetch workspace 都成功，崩在解析阶段）。
- 建议下一批：(1) 用最小化真实文件 + `PYTHONFAULTHANDLER`/`-X faulthandler` + 逐步削减定位触发的具体语法；
  (2) 让 `ParsedFile`/`SyntaxNode` 显式持有 `Tree`（与 `source` 字节）强引用，杜绝 child 节点悬空；
  (3) 补一条"对真实仓库样例目录索引不崩溃"的回归测试。
- 本项在修复前保持为**已知缺口**，不记为通过。
