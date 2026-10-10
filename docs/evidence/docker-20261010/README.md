# Phase 1 Docker 本机实测（2026-10-10）

Windows Docker Desktop Linux daemon 29.8.2 上运行真实 Linux 容器，退出码 0；外层 **1 passed / 0 skipped**，60.66s。
容器环境为 Python 3.12.7、uv 0.12.9、tree-sitter 0.25.2、tree-sitter-python 0.25.0；依赖由 uv.lock 冻结安装。
11 项 parser / golden / CLI / 增量与历史边界测试全部成功；额外对 mini_auth 执行真实 git、CLI、SQLite 验收，
得到 **14 Files / 58 Symbols / 200 Edges / 7 Tests / 0 解析错误**。
fixture 的实际 commit 为 `533cf280dcb2f3e908bc7ba4be8d0d3016b1ebb9`，不是主项目 commit。

[原始构建与运行日志](docker-final.log)、[命令/环境/退出码](docker-final.json)、[外层 JUnit](docker-local-results.xml)、
[SHA-256 与构建输入清单](manifest.json) 按字节复制自 `artifacts/ci-docker-20261010/`，原 artifact 保留。
构建上下文已逐文件核对与待提交工作树内容一致，只包含白名单代码和测试输入。

复跑：`uv run pytest -s -m docker`。构建阶段需要下载基础镜像和冻结依赖；运行阶段非 root、只读、无网络。
这是 Phase 1 索引验收；Phase 4 产品沙箱没有实施。后续 Ubuntu CI 结果见 [Phase 1 最新记录](../../notes/phase1.md)。
