# V-01～V-05 验收证据

- main-ci.json：PR #1 合并 SHA fd91cd5 的三个 CI job 均成功。
- ruff.log / format.log / mypy.log：最终代码静态门禁全部退出 0；使用项目 .venv 内工具（uv 的共享缓存受沙箱权限限制）。
- targeted.log：最终 25 项本批独立测试通过。
- pytest.log：首轮全量 781 passed / 1 deselected，覆盖率 94.07%；该轮收集后新增了一条路径引号/空异常回归，最终全量见 final-pytest.log/xml：782 passed / 1 deselected，覆盖率 94.33%。
- retrieve-*.json / stderr.log：mini_auth 真实 CLI 输出；smoke.json 包含 fixture commit、查询和两个 exit 0。
- environment.log：Python、依赖和 rg 版本。
- manifest.json：上述证据的 SHA-256；文件按二进制保留换行，避免跨平台 checkout 改写哈希。

源代码和接口约定见 ../../notes/phase2-retrieval.md。未执行真实 LLM、embedding 或产品沙箱验收。

- implementation-ci.json / implementation-ci-summary.log：实现提交 5d61201 的 PR CI 三个 job 全通过；Ubuntu 离线 777 passed / 5 Windows 专属 skip / 1 deselected，覆盖率 93.92%；Docker 1 passed / 0 skipped。完整原始 CI 日志保留在 artifacts/phase2-v01-v05/implementation-ci.log，远端运行 https://github.com/wanjiang263/CodePilot-Repair/actions/runs/38040385681 。

## 最终审查修复版本 9fb1802

- review-pytest.log/xml：本地非 docker/llm 785 passed / 1 deselected，覆盖率 94.31%。
- review-targeted.log：28 项独立测试通过；review-ruff/format/mypy.log 均 PASS。
- review-ci.json / review-ci-summary.log：PR CI 三个 job 全绿；Ubuntu 离线 780 passed / 5 Windows 专属 skip / 1 deselected，覆盖率 93.90%；Docker 1 passed / 0 skipped。运行 https://github.com/wanjiang263/CodePilot-Repair/actions/runs/38062687320 。
- review-retrieve-*.json / stderr.log：同一固定 mini_auth 提交上，两通道再次 exit 0。完整原始 CI 日志留在 artifacts/phase2-v01-v05/review-ci.log。
- 不带 review- 前缀的日志是初版实现提交 5d61201 的历史证据。
