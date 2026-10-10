# V-01～V-05 验收证据

- main-ci.json：PR #1 合并 SHA fd91cd5 的三个 CI job 均成功。
- ruff.log / format.log / mypy.log：最终代码静态门禁全部退出 0；使用项目 .venv 内工具（uv 的共享缓存受沙箱权限限制）。
- targeted.log：最终 25 项本批独立测试通过。
- pytest.log：首轮全量 781 passed / 1 deselected，覆盖率 94.07%；该轮收集后新增了一条路径引号/空异常回归，最终全量另行记录。
- retrieve-*.json / stderr.log：mini_auth 真实 CLI 输出；smoke.json 包含 fixture commit、查询和两个 exit 0。
- environment.log：Python、依赖和 rg 版本。
- manifest.json：上述证据的 SHA-256；文件按二进制保留换行，避免跨平台 checkout 改写哈希。

源代码和接口约定见 ../../notes/phase2-retrieval.md。未执行真实 LLM、embedding 或产品沙箱验收。
