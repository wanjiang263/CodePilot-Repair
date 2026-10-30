# Phase28 三层验证（2026-10-30）

本轮覆盖 L1 单元层与 L2 本地集成层，由 S6a 执行；L3 回归组由 S6b 并行执行、结果见文末占位节。验证对象为工作树中的 Phase28 实现（未提交）：新增 `src/codepilot/archive.py`、`scripts/phase28_archive.py`、`scripts/phase28_archive_verify.py`、`scripts/phase28_index_profile.py`，修改 `src/codepilot/repo/index.py`（`Indexer.__init__` 可选参数 `cache_max_entries=128` / `cache_max_bytes=8MiB`，默认行为不变），以及新单测 15+13+39+2 例。

约束与执行环境：零真实模型请求、零外部网络（仅 127.0.0.1 回环）、零费用、不用 Docker；未修改生产代码，未执行任何 git commit/push，未使用 skip/xfail/放宽断言。机器为 Windows 11（build 10.0.22631，Python platform 报告 `Windows-11-10.0.22631-SP0`）+ Git Bash，Python 3.12.7（`.venv`），pytest 9.1.1，git 2.51.1.windows.1；用户重启后环境干净，无残留进程。本轮所有日志写入 `.codex-pytest-tmp/phase28/s6a-*`，与并行 S6b 的 `s6b-*` 互不覆盖。

## L1 单元层

| # | 命令 | 退出码 | 结果 |
|---|---|---:|---|
| L1-1 | `.venv/Scripts/python.exe -m pytest tests/unit/test_phase28_archive.py tests/unit/test_phase28_archive_verify.py tests/unit/llm/test_phase28_http_matrix.py -q` | 0 | 67 passed in 65.78s（15 + 13 + 39） |
| L1-2 | `.venv/Scripts/python.exe -m pytest tests/unit/repo/test_index.py tests/unit/repo/test_resolution_cache.py -q` | 0 | 28 passed in 6.49s（含 Phase28 追加的 2 个缓存参数回归） |

日志：`.codex-pytest-tmp/phase28/s6a-l1-phase28-unit.log`、`.codex-pytest-tmp/phase28/s6a-l1-repo-index.log`（均以 `EXIT=` 结尾记录退出码）。

## L2 本地集成层

| # | 命令 | 退出码 | 结果 |
|---|---|---:|---|
| L2-1 | `.venv/Scripts/python.exe -m pytest tests/integration/phase28/ -q` | 0 | 7 passed in 13.12s |

日志：`.codex-pytest-tmp/phase28/s6a-l2.log`。一次通过，未触发偶发失败重跑（协议为固定随机端口重跑最多 3 次、禁止加大 timeout 掩盖）。回环假服务绑定 `127.0.0.1:0`（随机端口 + `allow_reuse_address`），本就没有固定端口冲突面。

### 集成测试清单（3 个文件 → 7 个测试 → 覆盖点）

`tests/integration/phase28/test_phase28_archive_verify_loop.py`（4 例）——archive→verify 数据完整性闭环，临时 git 仓库 + 临时 archive root：

- `test_generated_candidates_verify_clean_with_parent_chain`：base→v1→v2 两次提交后分别 `codepilot.archive.create_candidate`（v2 带 `parent=v1`）；断言候选目录恰为四文件布局（snapshot.json / candidate.json / sources.json / candidate.diff）、`schema_version==2`、`diff_base_commit` 逐版本正确；`scripts/phase28_archive_verify.py` 的 `verify_versions` 校验通过（`new_archive_status=passed`、2 个新档案 0 失败、v2 的 `parent_integrity_matches` 与 v1 实际复算一致）；并断言校验器只读——不产生 active 指针。
- `test_tampered_snapshot_fails_verification`：篡改 v1 的 snapshot.json（追加单字节）后校验退出码 1、`snapshot_file_sha256` 计入失败；同时断言 `snapshot_canonical_sha256` 不失败（canonical 哈希不受尾随空白影响）——两条哈希规则都真实生效，而非单点。
- `test_verify_cli_exit_codes_on_clean_and_tampered_archive`：subprocess 走真实 CLI 进程路径：干净档案退出 0 且输出 `new ok`；篡改 v2 的 candidate.diff 后退出 1 且输出 `[fail] diff_sha256`。
- `test_archive_cli_dry_run_prints_json_and_writes_nothing`：`scripts/phase28_archive.py --dry-run` 在真实仓库（`--allow-dirty` 记录工作树状态）上输出 JSON 摘要（含 `dry_run=true`、双哈希、目录路径）、退出码 0，且档案根零落盘（连 `candidates/` 都不出现）。

`tests/integration/phase28/test_phase28_index_storage_loop.py`（1 例）——索引+存储闭环，真 git 仓库 → WorkspaceManager fetch 工作区 → 磁盘 SQLite 文件：

- `test_index_storage_loop_with_default_and_capped_cache`：默认参数 `Indexer()` 冷索引 5 文件仓库，stats 与库行数互证（files/symbols/edges 行数、`association_mode=complete_closure_v2`、`tests==1`、SQLite 文件非空）；改 1 个文件后增量索引只重解析该文件、其余 3 个 py 走解析缓存复用、关联仍为完整闭包；随后 `Indexer(cache_max_entries=2)` 用**独立 SQLite** 复跑同一仓库（避开 `(url, base_commit)` UNIQUE 冲突），断言条数上界真实生效（`len(indexer._parsed)==2`）且 `dump_index` 与默认参数运行逐字段相等——缓存参数只影响内存容量、绝不改变索引语义；无变化快路径（`parsed_paths==()`）同样不受参数影响。

`tests/integration/phase28/test_phase28_registry_loopback.py`（2 例）——回环 HTTP + provider 结算闭环，`LLMRegistry` 生产入口 + 真 `HTTPTransport`（非 MockTransport）打本机假服务：

- `test_registry_live_request_over_loopback_settles_ledger`：Fake 凭据经 `api_key_env` 注入、provider 由 registry 按 LLMConfig 解析构造、`trust_env=False`；正常响应下 usage 解析（10/5 token）、dispatch ledger 结算为 `settled` 且费用精确等于 10×1.0 + 5×2.0 CNY/MTok = 0.00002 CNY；服务端恰好收到 1 次请求（无隐藏重试）。
- `test_registry_request_honors_total_deadline_over_loopback`：慢速 drip 响应体在总 deadline（2s，scope 按 `min(limit, remaining)` 语义补偿 0.8s）处被截断；账本冻结为 `unknown_frozen`、`cost_cny=None`、无退款；服务端确认客户端断开连接、恰好 1 次请求。断言为宽松上界（<8s）防 CI 抖动，未加大任何 timeout。

### 半成品文件的处理决定

上一轮被中断的 S6 尝试留下的 `tests/integration/phase28/test_phase28_archive_verify_loop.py`，经逐行核对（对照 `src/codepilot/archive.py` 的生成契约与 `scripts/phase28_archive_verify.py` 的逐项校验、失败注入点、退出码语义）判定：内容完整、自洽、数据完整性断言无放宽，中断发生在收尾报告阶段而非编码阶段。决定**原样采用**，未重写、未修补、未放宽任何断言；本轮运行结果（4 例全过）与其断言一致，佐证该判定。同目录另两个文件（index_storage_loop、registry_loopback）为同一轮遗留的完整文件，同样采用。

## L3 回归层

由 S6b 在最终实现代码上执行（与 S6a 并行，排除 Docker/swebench 与 S6a 工作目录）：

| # | 命令 | 退出码 | 结果 | 日志 |
|---|---|---:|---|---|
| L3-1 | `pytest tests/unit/llm/ -q` | 0 | 490 passed | `.codex-pytest-tmp/phase28/s6b-l3-llm.log` |
| L3-2 | `pytest tests/unit/repo/ -q` | 0 | 381 passed | `.codex-pytest-tmp/phase28/s6b-l3-repo.log` |
| L3-3 | `pytest tests/unit/storage/ tests/unit/api/ -q` | 0 | 39 passed | `.codex-pytest-tmp/phase28/s6b-l3-storage-api.log` |
| L3-4 | `pytest tests/unit/observability/ -q` | 0 | 53 passed | `.codex-pytest-tmp/phase28/s6b-l3-observability.log` |
| L3-5 | integration 13 文件合并（排除清单见下） | 见修复记录 | 首跑 99 passed / 4 failed，修复后该文件 18 passed | `.codex-pytest-tmp/phase28/s6b-l3-integration.log` |

排除清单：`test_docker_phase1.py`、`test_swebench_smoke.py`（需 Docker，本轮禁用）；`tests/integration/phase28/`（S6a 工作目录，其 7 例已计入 L2）。全部日志零 skip、零 xfail。

**测试数声明**：L3 各组合计 1066 次执行，与 L1/L2 存在目录重叠（如 tests/unit/repo、phase28 单测同时出现在两层），不跨层相加宣称覆盖。

### L3 首跑失败与修复记录（4 failed → 全绿）

- **第一现场**（保留于 `s6b-l3-integration.log`，未覆盖）：4 个失败均在 `tests/integration/test_retrieval_index_failures.py`——测试注入不可读文件后期望失败分类 `code="unreadable"`（及 `paths==["."]` 等），实际得到 `"index_execution"`。
- **定位**：`Indexer.index()` 在 `scan.unreadable` 非空时抛普通 `OSError`（phase26 提交 748ad2f 引入），retrieval runner 的通用 `except Exception` 把它包装成 `index_execution`，既有 `unreadable` 分类分支不可达。
- **证伪"非本轮引起"**：`git stash` 掉本轮 index.py 参数化改动后，在干净 HEAD（9ccc703）上单跑同一测试仍失败——确认是 phase26 遗留回归，Phase27 的受影响回归组（536 项）未覆盖该文件。
- **修复**（最小 diff，不改任何测试；`IndexingError` 契约与消息逐字节保持）：`src/codepilot/repo/index.py` 新增 `class IndexScanIncompleteError(OSError)`（携带 `unreadable` 路径属性，raise 站点改抛该类型，消息文本不变）；`src/codepilot/retrieval/runner.py` 在通用 `except Exception` 之前捕获该类型，经抽出共享的 `_unreadable_failure` helper 恢复既有 `unreadable` 分类（paths 取自异常携带路径，与 scanner 同源）。
- **调用方核查**：全仓 `Indexer(` 的 src 调用方 4 处（cli.py:439/929、eval/assembly.py:596、retrieval/runner.py:299）；新异常继承 `OSError`，所有 `except OSError` 语义不变；`pytest.raises(OSError, match="scan incomplete")` 等既有断言实测通过。
- **修复后回归**：该文件 18 passed；`tests/unit/retrieval/ tests/unit/repo/` 702 passed / 1 skipped（既有 embedding 模型门控，非本轮引入）；`tests/unit/api/ tests/unit/storage/` 39 passed。日志 `.codex-pytest-tmp/phase28/fix-unreadable-*.log`。

### 最终回归复跑（T11，提交源码上）与静态检查

在源码提交上重跑完整相关组（6/6 组全绿，日志 `t11-*.log`）：

| # | 命令 | 退出码 | 结果 |
|---|---|---:|---|
| A1 | `pytest tests/unit/llm/ -q` | 0 | 490 passed |
| A2 | `pytest tests/unit/repo/ -q` | 0 | 381 passed |
| A3 | `pytest tests/unit/storage/ tests/unit/api/ -q` | 0 | 39 passed |
| A4 | `pytest tests/unit/observability/ -q` | 0 | 53 passed |
| A5 | `pytest tests/integration/ -q --ignore=tests/integration/test_docker_phase1.py --ignore=tests/integration/test_swebench_smoke.py` | 0 | 110 passed（含 phase28 集成 7 例；排除 2 文件需 Docker） |
| A6 | `pytest tests/unit/test_phase28_archive.py tests/unit/test_phase28_archive_verify.py tests/unit/llm/test_phase28_http_matrix.py tests/integration/phase28/ -q` | 0 | 74 passed（组合复跑，与 A1–A5 部分重叠，不计入去重总数） |

去重总数 = **1073**（A1–A5 五组目录互不重叠，可相加）；A6 为组合交叉印证。静态检查：ruff check 通过；mypy（archive.py、index.py、runner.py）通过；web `tsc --noEmit` 与 `eslint src --max-warnings=0` 退出码 0；`git diff --check` 干净。**ruff format --check 首跑退出码 1**（3 个本轮文件待重排：`scripts/phase28_index_profile.py`、两个 phase28 集成测试）→ 主控按 format 重排这 3 个文件（仅风格），在最终字节上复跑 A6 口径 74 项全绿，随后将格式化 amend 进源码提交（51aae86 → 后续 770c0dc，见下）。
