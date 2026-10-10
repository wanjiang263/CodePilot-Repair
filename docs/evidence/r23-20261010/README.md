# R-23 可远端复核证据：2026-10-10 Windows 实测快照

本目录保存此前 `artifacts/r23-repair-20261010/` 的精选证据，供私有仓库中的远端审查使用。
原始日志、JSON、JSONL、源码快照按字节复制；原 artifact 未修改。历史日志中的 Windows 绝对路径、启动命令和时间戳保留原样，属于证据元数据，不是复跑命令。
本轮后续 Linux CI 与 Docker 验证另有记录，不能把这里的 Windows 成功结果记作 Linux 或 Docker PASS。
阶段最新状态见 [Phase 1 实测记录](../../notes/phase1.md)，执行流程见 [CI 配置](../../../.github/workflows/ci.yml)。

## 两个真实仓库的历史成功结果

| 仓库 | 固定 commit | Files | Symbols | Edges | Tests | 索引耗时 | 解析错误文件 | 扫描跳过 | 退出码 |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Flask 3.0.3 | `c12a5d874c5a014495eb2db8a73f40037bc813ac` | 241 | 1644 | 10839 | 384 | 19.31s | 0 | 8 | 0 |
| Requests 2.32.3 | `0e322af87745eff34caffe4df68456ebc20d9068` | 120 | 789 | 7059 | 333 | 9.81s | 0 | 11 | 0 |

[Flask result](flask-result.json)、[Requests result](requests-result.json) 保存分类、完整精度耗时及实际解析的 82 / 36 个 Python 文件路径。
所有 Python 文件均实际解析；未为通过验收跳过触发崩溃的源码。
扫描跳过沿用既有规则：Flask 的 8 个二进制文件；Requests 的 10 个二进制文件和 1 个超过 1MB 的文件。
Files 包括非 Python 文本文件，Tests 是静态索引计数，未执行 Flask / Requests 自身的测试套件。

## 崩溃根因与独立进程证据

历史环境：Python 3.12.7（Anaconda，MSC 1929，AMD64）、Windows 11 `10.0.22631`。
失败依赖组合为 `tree-sitter==0.26.0` + `tree-sitter-python==0.25.0`；通过组合为 `tree-sitter==0.25.2` + `tree-sitter-python==0.25.0`。

0.26.0 的 `Point.row` / `Point.column` getter 直接返回 `PyTuple_GetItem` 的借用引用，没有增加 Python getter 返回值所需的引用计数。
最小回归对同一个坐标对象检查引用数增量：取属性前后实测 `3 → 3`，正确应为 `3 → 4`。
CPython 的小整数缓存通常涵盖 `-5..256`；取 257 可避开缓存整数对问题的遮蔽。
临时 Point 或属性值的释放会使未正确计数的整数过早释放，导致内存损坏；最后崩溃帧可能漂移至 `Node.children`、`text` 或 GC。
`Node` 自身通过 `Py_NewRef(tree)` 持有 Tree 强引用；原生 API 对照全程保留 Tree 仍然崩溃，因此没有依据为 ParsedFile 添加冗余 Tree 引用。

- [上游 0.26.0 point.c](https://github.com/tree-sitter/py-tree-sitter/blob/v0.26.0/tree_sitter/binding/point.c) / [原始源码快照](upstream-point-0.26.0.c)。
- [上游 0.26.0 node.c](https://github.com/tree-sitter/py-tree-sitter/blob/v0.26.0/tree_sitter/binding/node.c) / [原始源码快照](upstream-node-0.26.0.c)。
- 两份上游源码快照的 [MIT 许可证](upstream-LICENSE.txt) 一并保留。
- [API 隔离](api-isolation.json)：Requests `models.py` 的原生 children 和 field/text 完成，坐标属性遍历退出 `0xC0000005`。
- [实际触发文件各模式 3 次复现](repeat-before.json)：Requests `models.py` / Flask `app.py` 的 extract 和坐标属性遍历各 3/3 崩溃；改用元组坐标下标后各 3/3 成功。
- [修复依赖下的真实源码对照](version0252-controls.json)：两个触发文件的符号提取和原生坐标遍历均成功。
- [最小源码版本矩阵](minimal-version-matrix.json)：257 个空行 + 两行函数，重复提取 100 次；旧版 3/3 崩溃、新版 3/3 成功，row/column 引用计数断言旧版各 3/3 失败、新版各 3/3 成功。
- [固定的最小子进程源码](minimal_child.py) 与 [跨平台复跑入口](replay_minimal.py)：子进程源码提取自当时新增回归的 `CHILD`，不依赖日后测试文件内容。

Windows 崩溃退出码为 `3221225477 / 0xC0000005`，faulthandler 输出为 `Windows fatal exception: access violation`。
其他操作系统的原生崩溃退出码可能不同，必须记录实测值。

## 完整逐文件探针

每文件独立进程；每行 JSONL 保留 stdout、faulthandler stderr、退出码、Python / Windows / 依赖版本、源码 SHA-256。
修复前后逐文件源码 SHA-256 一致；成功索引的 parsed_paths 与完整探针文件集合一致。

| 仓库 | 旧版崩溃 | 修复后崩溃 | 完整原始记录 |
|---|---:|---:|---|
| Flask | 21/82 | 0/82 | [before](flask-before.jsonl)、[after](flask-after.jsonl) |
| Requests | 9/36 | 0/36 | [before](requests-before.jsonl)、[after](requests-after.jsonl) |

[探针汇总](probe-summary.json)。旧报告称 Requests 10/36，但其原始输出缺失，不能补造；9/36 是本轮历史实测。
[probe.py](probe.py) 仅调整了定位项目根目录的层级，其余探针逻辑保留；它支持 children、fields、points、tuple-points、extract 模式。

## 失败测试、成功门禁与性能

- [旧版失败输出](target-before.log) / [退出码与环境](target-before.json)：3 failed / 1 passed，包含最小符号提取崩溃的 faulthandler。
- [修复后输出](target-unsandboxed.log) / [退出码与环境](target-unsandboxed.json)：6 passed，涵盖 4 项 native subprocess 回归及 2 项 repo + commit 幂等测试。

历史正式门禁按以下顺序执行，所有退出码为 0。`.log` 是原始终端合并输出，`.json` 保存确切命令、工作目录、耗时、时间戳与环境。

| 顺序 | 检查 | 原始输出 | 退出码和环境 |
|---|---|---|---|
| 1 | ruff check | [log](gate1-ruff.log) | [json](gate1-ruff.json) |
| 2 | ruff format --check | [log](gate2-format.log) | [json](gate2-format.json) |
| 3 | mypy src | [log](gate3-mypy.log) | [json](gate3-mypy.json) |
| 4 | 非 docker、非 llm pytest 覆盖率 | [log](gate4-pytest.log) | [json](gate4-pytest.json) |
| 5 | mini_auth CLI | [log](gate5-mini-auth.log) | [json](gate5-mini-auth.json) |
| 6 | Flask 真实索引 | [log](gate6-flask.log) | [json](gate6-flask.json) |
| 7 | Requests 真实索引 | [log](gate7-requests.log) | [json](gate7-requests.json) |

Windows 历史全量离线测试为 **752 passed / 覆盖率 94.21%**，不预设后续 Linux 的通过或跳过数量。
fixture CLI 为 14 Files / 58 Symbols / 200 持久化 Edges / 7 Tests；golden 为 58 Symbols / 203 原始 Edges，差异来自既有持久化去重。
fixture 使用字节一致的隔离副本，实际 commit 为 `a1f1dd7b04161d2d8be73e2fb979a86d2a8a35de`；[fixture 元数据](fixture.json) / [逐文件字节核对](fixture-verified.json)。

[性能原始样本](performance-comparison.json)：6 个独立进程按 ABBAAB 顺序比较 0.26.0 / 0.25.2，各轮预热 10 次、测量 100 次。
mini_auth 解析与 symbol 提取的“各轮中位数的中位数”为 **5.507 → 5.469ms**，比例 **0.9932**，该次比较未观察到退化。
这不是全平台性能保证，也不含真实仓库索引的 Git / SQLite 开销。

## 从项目根目录复跑

以下命令使用 `uv run` 选择项目虚拟环境，适用于 Windows 与 Linux；无需使用历史日志中的 `.venv/Scripts/*.exe` 路径。

```text
uv sync --frozen --extra dev
uv run python docs/evidence/r23-20261010/replay_minimal.py --output artifacts/r23-minimal-current-new.json
uv run python docs/evidence/r23-20261010/replay_minimal.py --compare --temp-root artifacts/r23-replay-temp-new --output artifacts/r23-minimal-comparison-new.json
uv run pytest tests/unit/repo/test_parser_subprocess.py tests/unit/test_index_real_repo.py
```

`--compare` 将两个精确绑定版本安装至独立临时目标目录，用当前环境的 grammar 0.25.0，保持项目 `.venv` 的锁定依赖不变。
这一步需要联网下载绑定；uv 不在 PATH 时给复跑脚本追加 `--uv <path-to-uv>`。
Windows 沙箱中可以用 `--temp-root` 显式指定可访问的临时目录父目录。
每个用例仍通过独立子进程运行。输出文件必须使用新路径，脚本拒绝覆盖既有文件。
对照脚本退出 0 表示观察到预期旧版失败与新版成功，逐子进程退出码仍分别保留于报告。

真实索引复跑：

```text
uv run python scripts/index_real_repo.py --repo https://github.com/pallets/flask.git --commit c12a5d874c5a014495eb2db8a73f40037bc813ac --workdir artifacts/r23-flask-new
uv run python scripts/index_real_repo.py --repo https://github.com/psf/requests.git --commit 0e322af87745eff34caffe4df68456ebc20d9068 --workdir artifacts/r23-requests-new
```

探针应指向已 checkout 对应 commit 的源码目录，而不能对 bare 对象目录计数。上面索引脚本用完会移除临时 workspace；如需逐文件复跑，先另建源码 checkout：

```text
git clone https://github.com/pallets/flask.git artifacts/r23-flask-source-new
git -C artifacts/r23-flask-source-new checkout --detach c12a5d874c5a014495eb2db8a73f40037bc813ac
uv run python docs/evidence/r23-20261010/probe.py --repo artifacts/r23-flask-source-new --out artifacts/r23-flask-probe-new.jsonl
```

使用新的输出路径和 workdir，保留历史证据。真实索引脚本会更新 `docs/notes/phase1.md` 中对应 repo + commit 的结果段落；本目录快照不变。

## 文件完整性与范围

[manifest.json](manifest.json) 保存本目录除 manifest 自身外每个文件的 SHA-256、字节数及原始来源。
manifest 不将自身哈希写入自身，避免递归定义。复制源为历史 artifact 的文件，其源哈希和目标哈希完全一致；复跑入口与说明文件单独标为新增。
本目录的 `.gitattributes` 禁止自动换行转换，保证不同系统 checkout 后仍能按原始字节核对 SHA-256。
未复制仓库 clone、绑定目标目录、数据库、`.venv` 或凭据；原始证据不做静默脱敏改写。
当前目录进行过密钥模式扫描，结果见 [check.json](check.json)；没有发现候选密钥。
