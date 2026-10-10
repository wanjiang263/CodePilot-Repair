# Ubuntu CI 实测证据（2026-10-10）

[成功的 push 运行](https://github.com/wanjiang263/CodePilot-Repair/actions/runs/38037605606) 测试提交
`16dfa9b184ee7489617eddfce44a143b44ac432c`；[同提交的 PR 检查](https://github.com/wanjiang263/CodePilot-Repair/actions/runs/38037648144) 也成功。
以下文件按字节下载自该 push 运行的三个 GitHub Actions artifact，不使用 Windows 的计数替代 Linux 结果。
记录的是该提交的已完成运行，后续文档提交的检查状态以 PR 最新 checks 为准。

| 检查 | 结果 | 原始证据 |
|---|---|---|
| ruff check / format / mypy | 全部成功 | [quality-evidence](quality-evidence/ruff-check.log)、[format](quality-evidence/ruff-format.log)、[mypy](quality-evidence/mypy.log) |
| 离线 pytest + coverage | 747 passed / 5 skipped / 1 deselected；93.77% | [完整输出](pytest-evidence/pytest.log)、[JUnit](pytest-evidence/pytest-results.xml) |
| Docker 标记验收 | 1 passed / 0 skipped / 752 deselected | [完整构建与运行输出](docker-evidence/docker-test.log)、[JUnit](docker-evidence/docker-results.xml) |

5 项 skip 全为 Windows 专属路径（taskkill / junction），逐项原因见 pytest 输出；Linux 符号链接测试已执行。
Docker job 的 daemon 检查和 JUnit 非空/零跳过门禁均通过。容器内 11 项既有合同测试全部通过，
真实 mini_auth CLI + SQLite 为 14 Files / 58 Symbols / 200 Edges / 7 Tests / 0 解析错误。
本轮 Ubuntu 容器 fixture 的实际 commit 为 `70fff789a6edbce33e87fda34475661867aa36bb`，由容器创建并回显，未使用主项目 commit。

宿主运行环境：Linux x86_64，Python 3.12.14、tree-sitter 0.25.2、grammar 0.25.0、Docker 28.0.4。
容器运行环境：Python 3.12.7、uv 0.12.9、同一冻结解析依赖。
[环境](pytest-evidence/environment.log)、[daemon](docker-evidence/docker-info.log)、[完整任务结论与步骤](github-run.json)、
[SHA-256 与 JUnit 计数](manifest.json) 可供核对。全部三个 job 的结论为 success。

原始下载与完整任务日志保留于本地 `artifacts/ci-docker-20261010/`；旧 R-23 artifact 未修改。
这是 Phase 1 索引验证，未实现 Phase 4 产品沙箱。
