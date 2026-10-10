# Phase 2 第一批接口冻结（V-01～V-05）

基线：PR #1 合并提交 fd91cd5；范围仅规则查询、symbol 文档、ripgrep 与 BM25。

- `build_query(issue, repo_files=(), repo_root=None) -> RetrievalQuery`：有序去重标识符/关键词/报错/路径/测试名；原始帧文本仍在 traceback_frames，新增 parsed_frames 复用 TraceFrame（保留重复帧与顺序）。只有与索引文件对应的相对路径或 repo_root 内绝对路径才标 in_repo（Windows 根路径忽略大小写并返回索引拼写，POSIX 保持大小写敏感）；不按文件名猜测第三方帧。
- `build_documents(root, symbols) -> list[SymbolDocument]`：每个 symbol 一篇，包含 path/symbol/signature/docstring/code；按 symbol 完整行范围截取，超过 60 行保留前 40、后 10，加截断标记。类和 module 也按相同上限，避免重复全文膨胀。拒绝越界与链接路径。
- `BM25Channel.search(query)` / `RipgrepChannel.search(query) -> list[RankedSymbol]`：独立通道，候选唯一键 symbol_id。每个候选保留原始分和 query 内 min-max 分（相等时全 1）。排序使用原始分降序，再 file、start_line、qualified_name、symbol_id 升序。零/负 BM25 分仍可有效，必须匹配至少一个查询词，不能把无命中文档补零混入排序。
- BM25：rank_bm25.BM25Okapi，k1=1.5、b=0.75、epsilon=0.25；代码标识符整体转小写，同时追加驼峰/下划线/点号拆分词；文档和查询共用分词。文档内词频保留，查询词有序去重。
- ripgrep：大小写敏感固定字符串搜索，仅搜索索引中有 symbol 的文件。每个命中归属到最小行范围 symbol，避免 module/class 吞掉函数命中；同范围时优先函数/方法，其次 class，最后 module。原始分 = 不同命中词数 + 4（symbol 简名或全名精确命中）+ 3（错误字符串精确命中）；两个 bonus 各最多一次。元数据名称命中也可返回候选。空查询无候选；缺 rg/超时/错误明确抛 RetrievalError。
- `retrieve` 验收入口一次选择 bm25（默认）或 ripgrep；`fusion_method=none`，`symbols` 是所选通道 Top-K，`per_channel` 保留完整候选，`score=raw`，`channel_weights={}`。files 留空（V-19 未实施），pinned=false（V-18 未实施）。本批没有把两组不同量纲原始分相加。
- 不实现 V-06 起的 embedding、结构/历史/测试通道、融合/置顶/文件聚合，也不启动 Phase 3、真实 LLM 或产品 Docker sandbox。

ripgrep 参数与 JSON match 格式已通过 Context7 核对官方源码：https://github.com/BurntSushi/ripgrep/tree/master/crates/printer/src

## 验收记录（2026-10-10）

PR #1 收口：修复不可读扫描路径误删、64 位 SHA 历史解析、旧 edges 表迁移；针对性 38 passed，全量离线 758 passed / 1 deselected。合并提交 fd91cd5；[main CI](https://github.com/wanjiang263/CodePilot-Repair/actions/runs/38039498393) 的 quality、offline pytest、Phase 1 Docker 全绿。仓库仍为 private。

初版独立测试 25 passed：traceback 顺序及重复帧、路径引号与空错误信息、仓库内外路径、camel/snake 拆词、60/61 行截断、越界/链接路径、BM25 手算数值及顺序、空语料/零匹配/负分、稳定排序、真实 rg 特殊字符/空格路径/未索引文件/最内层 symbol、缺 rg/失败/超时，以及两个通道的真实 mini_auth CLI。

真实 retrieve 验收（非模拟）：从 mini_auth 新建独立 fixture Git 仓库，在固定提交上执行以下命令，两条均 exit 0。完整 JSON、stderr、fixture commit 和最终质量门禁见 `docs/evidence/phase2-20261010/`，本地原始产物保留在 `artifacts/phase2-v01-v05/`。

```text
codepilot retrieve "Session.login password" --repo <fixture> --base-commit <固定 SHA> --channel bm25 --limit 5
codepilot retrieve "Session.login password" --repo <fixture> --base-commit <固定 SHA> --channel ripgrep --limit 5
```

此次 BM25 首位是 `tests.test_session.TestSession`，ripgrep 首位是 `tests.test_session.TestSession.test_login_with_correct_password`；`miniauth.session.Session.login` 在 ripgrep 第 3 位。这里只验证可运行性和接口，不宣称检索质量提高；尚未实现的融合、traceback 置顶和 Recall@K 评估均不算本批成果。

限制：自然语言采用 Unicode 词抽取与英文停用词表，未提供中文分词器；绝对 traceback 路径只能在当前 workspace 根目录下精确映射；CLI 目前每次建立内存索引，无持久检索缓存或完整 snapshot 管理。旧 RetrievalQuery.traceback_frames 和 RetrievalResult 既有字段保持兼容。

初版实现提交 `5d61201` 验收：本地非 docker/llm **782 passed / 1 deselected / 覆盖率 94.33%**；ruff check、format check、mypy src 全 PASS。实现提交 `5d61201` 的 [PR #2 CI](https://github.com/wanjiang263/CodePilot-Repair/actions/runs/38040385681) 三个 job 全绿：Ubuntu 离线 **777 passed / 5 Windows 专属 skip / 1 deselected / 覆盖率 93.92%**，Phase 1 Docker **1 passed / 0 skipped**。本地本批未重复运行 Docker；CI 已执行既有真实验收，未新增产品 sandbox。

## PR #2 审查补充

修复未加引号的 PascalCase 类名原始拼写丢失、Windows traceback 大小写误判，并通过真实 rg 回归发现并修复 module/class 同范围时的命中归属。最终针对性 **28 passed**；ruff/format/mypy 全通过。旧日志保留为初版证据，审查修复后的日志使用 review- 前缀。

最终审查修复提交 `9fb1802`：本地非 docker/llm **785 passed / 1 deselected / 覆盖率 94.31%**，28 项独立测试、ruff、format、mypy 均 PASS；[最终代码 PR CI](https://github.com/wanjiang263/CodePilot-Repair/actions/runs/38062687320) 三个 job 全绿，Ubuntu **780 passed / 5 Windows 专属 skip / 1 deselected / 覆盖率 93.90%**、Docker **1 passed / 0 skipped**。两条 PR 审查线程已解决，两个 mini_auth retrieve 命令再次 exit 0；最终证据为 `docs/evidence/phase2-20261010/review-*`。
