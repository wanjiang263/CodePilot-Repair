# Phase28 S4：缓存与搜索收敛分析（2026-10-30）

只读分析轮。未改任何生产代码/测试/配置；实验脚本在 `.codex-pytest-tmp/phase28/s4/`（已被 gitignore，不提交）。零网络、零模型请求、零费用。

结论先行：

1. **解析缓存（Indexer._parsed）**：130 文件用例仍成立（既有测试通过 + 微实验复现 2/128）；"130 扫描不逐出 128 命中"是快照语义的直接结果。
2. **抖动**：2589/128（而非 0/128）正是 `cached_at_start` 固定快照（index.py:212）的产物；Phase27 报告的 644 逐出是"冷 322 + 增量 322"的进程内累计值，每轮逐出数 = 每轮缺失数（微实验证实）。
3. **QueryCache**：hit/miss/bypass 三条路径行号齐全；范围搜索旁路是因为无可信外部修订号时只能全量内容哈希，护栏（2000 文件/50ms/1MiB）触发即旁路并记忆（`bypassed_scopes`），第二次同范围搜索复用该决策、不重复身份扫描（微实验用禁用 `os.walk` 证明）。
4. **旁路安全性**：旁路永不读缓存条目，永远执行真实引擎，不存在陈旧返回；新增发现——ripgrep 多线程下截断搜索（>50 命中）的 50 条子集本身在重复调用间可能不同（无 `--sort`），而缓存只存非截断完整结果（query_cache.py:246），因此**缓存命中比旁路更"一致"**，旁路是安全兜底而非缺陷。
5. **三选一裁定（范围搜索缓存）**：**继续旁路**（对大范围/全局），小范围缓存维持现状（已启用且安全）。依据见 §5。
6. **解析缓存容量**：**保持 128 默认**。512/不限在合成子集上省 ~11% 墙钟但解析本身只占 1.65%，且该子集 ~1KB/文件让 8MiB 字节上限形同虚设；微实验证明 ~200KB/文件的 Django 形态下字节上限先于条数上限生效（41 条封顶），只改条数上限对 Django 无效，改字节上限才是无界内存风险。构造参数（本轮已授权）保留为剖析专用 opt-in。

---

## 1. 解析缓存 Indexer._parsed

| 项 | 事实 | 证据 |
|---|---|---|
| 结构 | `OrderedDict[tuple[str,str,str], tuple[ParsedFile,int]]`，实例内、不持久化 | index.py:85 |
| 容量 | 条数 ≤ `cache_max_entries`（默认 128）**且**源字节和 ≤ `cache_max_bytes`（默认 8MiB）；两者任一超限即从最旧端逐出 | index.py:82-83、91-92、231-235（本轮授权的可选参数，默认值与原硬编码 128/8MiB 完全一致） |
| 键 | `(str(root.resolve()), 相对路径, 内容 sha256)` —— 内容变 = 新键 | index.py:216 |
| 插入 | 仅源 ≤1MiB 的文件入缓存 | index.py:229 |
| 触碰 | 命中且键仍在 live cache 时 `move_to_end`（真正的 LRU touch） | index.py:220-221 |
| 失效 | 无显式失效：内容变化=新键（旧条目成为不可达垃圾，等 LRU 逐出）；文件 >1MiB 永不入缓存；跨实例/跨进程天然冷（仅数据库复用不加载解析树）；扫描中内容漂移直接抛 `OSError` 终止 | index.py:216-227；phase27-runtime-validation.md:66 |
| 消费方式 | `cached_at_start = dict(self._parsed)` 固定快照后按扫描序查找；**每次增量重建需要全部树**（`parsed_files` 覆盖全部扫描文件 → `AnalysisContext` → `_extract_all_edges` 全仓库完整关联闭包） | index.py:212-236、256、282、490-496；模块 docstring index.py:1-8 |

context.py / imports.py **没有**跨运行的解析缓存：`AnalysisContext`（context.py:57-88）与 `ImportResolver`（imports.py:12-31）是一次性索引内对象，索引结束即弃。`tests/unit/repo/test_resolution_cache.py` 测的是 `NameResolver._resolution_cache/_mro_cache`（resolution.py:31、33），它们绑定单个 context 实例、不跨索引存活（测试第 23-28 行显式新建 context 验证无泄漏），与 `_parsed` 是两层不同缓存。

**130 文件用例仍成立**（两次独立证据）：

- 既有测试 `tests/unit/repo/test_index.py::test_scan_does_not_evict_every_initial_cache_hit`（57-69 行）本轮实跑：`6 passed in 5.41s`。
- 微实验 exp1 场景 A（同参数独立复现，附逐出计数）：

```
== 场景A：130 文件，cache_max_entries=128（默认，与既有测试同参数） ==
[cold] parsed=130 reused=0 changed=130 evictions=2  entries=128 bytes=3584
[incr] parsed=2  reused=128 changed=1   evictions=4  entries=128 bytes=3584
  incr 命中键范围: mod002.py .. mod129.py  数量=128
  incr 重解析键: ['mod000.py', 'mod001.py']
```

即：冷跑后缓存恰为扫描序最后 128 个键；增量轮快照命中这 128 个，只有被改的 mod000 与被逐出的 mod001 重解析。evictions 计数器跨轮累计（与剖析脚本同机制）：冷 2 + 增量 2 = 4，**每轮逐出数 = 该轮缺失数**（每条插入在满容量时触发一次逐出）。

## 2. 大仓库增量扫描的缓存抖动：为什么是 2589/128 而不是 0/128

完整闭包设计（index.py:1-8 docstring）决定了增量重建要吃下**全部**解析树：`parsed_files` 覆盖所有扫描文件（index.py:236），`new_symbol_ids` 是全仓库符号（277-280），`_extract_all_edges` 对全 context 跑四类提取（282、490-496）。因此：

- **解析数 = Python 文件数 − 缓存命中数（≤128）**。Django：2717 − 128 = 2589（phase27-runtime-validation.md:63）；合成子集：450 − 128 = 322（phase28-index-profile.json `q1_2589_parses`，observed_cache_128: parsed=322 / parse_reused=128）。
- **为什么不是 0/128**：修复前的循环复用判定直接查会被插入逐出的 live cache——扫描前 128 个缺失的插入就把所有待复用条目逐光，后面全部落空（0 命中）。修复 = index.py:212 的 `cached_at_start = dict(self._parsed)` 固定快照，命中判定不再受本轮插入的逐出影响（phase27-runtime-validation.md:68）。
- **phase28-index-profile.json 的 644 逐出是累计值**：`scripts/phase28_index_profile.py:482-486` 的 CountingLRU 在子进程内跨"冷基线 + 增量实测"共享计数器（child-cached 模式，:608-617），冷跑 450−128=322 + 增量 322 = 644；q3 verdict"逐出数≈逐出插入数=缺失数，命中来自快照，与逐出不互斥"与此一致。微实验逐轮拆分证实。
- **旧算法 vs 现算法对照**（exp2，N=300、cap=128）：

```
== 现行实现（cached_at_start 快照，index.py:212） ==
[cold] parsed=300 reused=0   evictions=172
[incr] parsed=172 reused=128 evictions=172  (逐出=本轮缺失数)
== 修复前算法模拟（复用判定查 live，无快照） ==
[incr] parsed=300 reused=0   evictions=300
```

外推到 Django：现行为 2589/128（Phase27 实测复现），旧算法为 2717/0。快照把增量解析数从 N 压到 N−128，但**没有**把关联重建压到局部（后者才是 142.7s 增量 > 105.0s 冷索引的主因，phase27-runtime-validation.md:63、72）。

## 3. QueryCache：容量、键、三条路径

**容量**（query_cache.py）：响应条目 ≤64 条且 ≤8MiB（28、261-269），单条 ≤2MiB（261 `max_bytes//4`）；`identities` ≤64（185-186）；`read_ranges` ≤64（306-307）；`bypassed_scopes` ≤64（77）。

**键**：`sha256([QUERY_VERSION, 源码版本哈希, engine(rg 路径/大小/mtime 或 python_fallback), tool.name, tool schema, args, permissions(root/role/deny 清单), content_identity, epoch])`（39-44、144-156、187-203）。版本哈希覆盖 query_cache.py+read_tools.py+base.py 三个源文件字节（38-44）——代码升级自动全量失效。`request_key`（不含 identity/epoch，164-177）仅用于识别"同请求、内容变了"并给出 `workspace_content_changed` 诊断（178-184）。

**content_identity 判定**（54-139）：
- read_file：`resolve(must_exist)` + `verify_identity`（58-59）后对内容 + `st_mode` 做 sha256；>8MiB 返回 None（60-66）。
- search_code：`path_glob` 前缀推导范围目录（71-73）；**无范围（全局）直接 None**（74-75，注释 69-70：无可信外部修订时 mtime/size 不足以做内容身份）；范围已被记忆旁路或旁路集满 64 → None（76-77）；祖先 `.gitignore/.ignore/.rgignore` 与 `.git/info/exclude` 全部哈希进身份（89-102）；`os.walk` 中被拒目录剔除（105-112）、**>2000 文件或 >50ms → 旁路并记忆**（116-118）、符号链接 → None（119-120、136-138）、累计 >1MiB → 旁路并记忆（129-132）、不可读文件以 `"unreadable"` 参与哈希（126-128，权限变化也改变身份）。

**三条路径触发行**：

| 路径 | 触发点 | 行为 |
|---|---|---|
| hit | `identity is not None and key in self.entries` | query_cache.py:204-217；`hits+=1`，深拷贝 + 标注 `hit=True`，`move_to_end`；**每次 hit 仍先付身份扫描成本**（identity_seconds>0，见 exp3） |
| miss | 上述不成立 → `tool.execute`（221） | query_cache.py:218-244；`misses+=1`；结果分类 complete/empty/truncated/missing（223-234）；仅当 `identity 存在 ∧ ok ∧ 非截断`（246）且身份复核稳定（249-252）、成本合算（255）、体积 ≤2MiB（261）才入库（261-269）；身份成本 ≥ 搜索成本时把范围加入旁路集并标 `identity_cost_exceeds_search`（256-260） |
| bypass | `_content_identity` 返回 None（上表任一护栏/全局） | 不查缓存、不入缓存，永远执行真实引擎；reason=`identity_unavailable_or_scope_limit`（235） |

**范围搜索为什么旁路（正确性理由）**：没有可信的"外部修订号"可用（69-70 注释明说 mtime/size 不够），唯一安全的身份是全量内容哈希；而大范围全量哈希本身是 DoS（时间/内存无界），所以设 2000 文件/50ms/1MiB 三道护栏，触发即放弃缓存改走原始引擎——**宁可慢也不冒陈旧风险**。

**第二次相同范围搜索复用旁路决策**：`bypassed_scopes` 检查在 walk **之前**（76 行早于 105 行），所以第二次起不再扫描。既有测试 `test_expensive_scope_is_not_rescanned`（test_phase26_closeout.py:161-173）用 monkeypatch 断言；exp3 在真实文件上复现：

```
== 3) 大范围旁路（many/ 2101 个文件 > 2000 上限，query_cache.py:116-118） ==
  call1: identity_s=63.0  search_s=844.0  bypassed_scopes=['many']
  call2: identity_s=0.0   search_s=829.0  hit 仍为 False
  call3(os.walk 禁用): 成功返回 → 旁路决策已复用，未重复身份扫描；search_s>0 → 真实搜索仍执行
```

exp3 的 63ms 身份扫描与 Phase27 实测 62ms（phase27-runtime-validation.md:74）同源：50ms 走查上限（116 行）+ 上下文开销。Phase27 的 281.90/162.95ms 两次范围搜索都是 bypass 路径，第二次省掉的只是 62ms 身份扫描，不是搜索本身——"第二次更快"≠"缓存命中"，本报告维持这一区分。

## 4. 搜索收敛

- **相同范围重复搜索一致性**：非截断结果确定性一致——rg 结果按 `(path,line,column)` 排序（read_tools.py:313）、fallback 同样排序（227）且文件序确定（139-159）；缓存命中返回同一响应的深拷贝（query_cache.py:206），matches 逐项相等（exp3 §2：12==12 全等）。**截断结果（>50 命中）例外**：`read_tools.py:292-294` 取 rg stdout 前 50 行，而 rg 默认多线程无 `--sort`，重复运行输出序不同——独立验证：同命令 3 次运行 `DIFFERENT`，`-j1` 后 `SAME`（命令见 §7）。因此截断子集在重复调用间可能合法地不同。
- **修订身份变化后的失效路径**（三层，全部实测）：① 任何非 READ 工具执行后 `registry.py:114-115` 调 `invalidate` → `epoch+=1`（query_cache.py:46-52），epoch 在键里（198），全部条目立即不可达（exp3 §5：epoch 0→1，同参数搜索 miss，reason=`workspace_side_effect:edit_file`）；② 外部改动（不经工具）：身份重算不同 → 新键 miss，reason=`workspace_content_changed`（178-184），且旧值绝不返回（exp3 §1 call3、§2 call3：新内容/新文件出现在结果中，旧命中全保留）；③ 大范围：每调用重走护栏 → 旁路。
- **旁路是否安全（会不会返回陈旧结果）**：**安全**。bypass 路径不触碰 `entries`（命中要求 `identity is not None`，204 行；入库要求同，246 行），旁路范围永远实时执行 rg/fallback。缓存侧的陈旧防护：身份含全部内容与 ignore 文件字节（97-98、133-135），失败与截断结果不粘滞（245-246 注释 + exp3 §1 call4：删除文件后 `ok=False error=not_found kind=missing`，经 base.py:324-336 的 failure 包装，未缓存）。唯一理论死角——`identities` 只存 64 个 request_key（185-186），被挤出后 reason 诊断退化为 `last_invalidation`，但键本身仍含 identity，**只影响诊断字符串，不影响正确性**。

## 5. 裁定：范围搜索缓存 = 继续旁路（大范围/全局），小范围维持已启用现状

现状其实已是"分级启用"：小范围（护栏内、身份成本 < max(5ms, 搜索成本)）**已经缓存**（exp3 §2 命中实测）；大范围/全局旁路。Phase27 观察到的正是大范围 case。

**继续旁路的依据**：

- **收益上限太小**：旁路下重复搜索成本 = 真实搜索 162.95ms（Phase27 第二次，phase27-runtime-validation.md:74）；启用大范围缓存的理论最优 = 身份全量哈希 + 查表。而那个范围的身份数字化成本本身就是 62ms（占第二次搜索的 38%），且这 62ms 是撞了 50ms 护栏被截断的——完整哈希会更贵。净节省 <60%，只惠及"同范围精确重复查询"，exp3 实测大范围搜索一次 829-844ms、身份一次 63ms。
- **正确性前提不存在**：要便宜且安全，需要可信 workspace 修订号（fs watcher / git 快照对比），当前架构明确拒绝用 mtime/size 充当（query_cache.py:69-70）。补齐该前提是新机制而非补测试。
- **旁路零陈旧风险**（§4），且截断结果本就因 rg 并行序在重复调用间波动（§4 新发现）——缓存完整结果反而比旁路更一致，这进一步支持"只缓存完整结果、大范围保持旁路"的现状。
- **若未来要启用**，条件：可信修订号源 + 修订号变更测试（改动→失效）、并发写竞争测试、护栏绕过审计（防 64 个旁路集被刷满后误判），并重跑 Phase27 同款双搜索对照。

## 6. 解析缓存容量：保持 128 默认（8MiB 字节上限不动）

phase28-index-profile.json 数据（合成子集，450×~1KB，免责声明明示不可外推绝对耗时）：

| cache | wall | parsed/reused | 逐出 | parse 占比 | 峰值工作集 |
|---|---|---|---|---|---|
| 128 | 2.5694s | 322/128 | 644(累计) | 0.0425s / 1.65% | 122.36MB |
| 512 | 2.2763s | 1/449 | 0 | 0.0001s | 122.93MB（+0.55MiB） |
| 不限 | 2.3643s | 1/449 | 0 | 0.0001s | 123.07MB（+0.69MiB） |

- 子集上 512 省 0.29s 墙钟（~11%），但 **parse 本身只差 0.0424s**；且不限（2.3643）反而慢于 512（2.2763）——0.088s 的波动与"节省"同量级，11% 里大半是二级效应/噪声。真正的 wall 大头是完整关联闭包（extract_all_edges 45.5-49.6%，加 context/imports 等关联侧合计约 66%），缓存容量根本不触碰它。
- **Django 形态下 8MiB 字节上限先绑定**：exp1 场景 C（130×~200KB，总源 26MB，默认 128/8MiB）实测 `entries=41, bytes=8,200,779`（≈8MiB 封顶），冷逐出 89、增量 parsed=89/reused=41——条数上限 128 根本没参与。2717 个更大的 Django 文件同理：**只把 128 改成 512 对 Django 几乎无效**；要生效就得同时抬字节上限，那才是未评估的无界内存风险（Phase27 已明确拒绝：phase27-runtime-validation.md:72；峰值工作集已在 1.2GiB 量级）。
- 收益/风险比：子集 11% 不可外推；Django 增量的解析节省上界 = 2589−(2717−上限) 棵树的解析时间，而 parse 在 128 配置下只占 wall 1.65-2.45%（profile q2/scenarios）。
- **裁定**：默认保持 `cache_max_entries=128, cache_max_bytes=8MiB`（index.py:82-83）。新参数仅留给剖析/未来受控实验 opt-in；全部生产调用点（cli.py:439、929，eval/assembly.py:596，retrieval/runner.py:299）都用默认构造，行为不变。若将来要调，先用真实 Django 工作负载测"条数+字节"两个参数的联合收益与峰值内存，再动默认值。

## 7. 实验命令与输出

环境：`.venv\Scripts\python.exe`（Python 3.12.7，Windows）。脚本（不提交）：`.codex-pytest-tmp/phase28/s4/`。

```
./.venv/Scripts/python.exe .codex-pytest-tmp/phase28/s4/exp1_parse_cache_lru.py
    # 输出：场景A 130文件默认上限 cold 130/0 evict2 → incr 2/128 evict累计4；命中键 mod002..mod129
    #       场景B 512上限 cold 130/0 evict0 → incr 1/129 evict0（entries=131）
    #       场景C 130×200KB 默认上限 entries=41 bytes=8,200,779（字节上限先绑定）incr 89/41
./.venv/Scripts/python.exe .codex-pytest-tmp/phase28/s4/exp2_thrash_snapshot.py
    # 输出：现行(快照) incr 172 parsed/128 reused/172 evict；旧算法模拟 300/0/300（N=300, cap=128）
./.venv/Scripts/python.exe .codex-pytest-tmp/phase28/s4/exp3_query_cache.py
    # 输出：read_file miss(16ms)→hit(0ms)→改内容 miss(reason=workspace_content_changed)→删除 failure not_found
    #       小范围搜索 miss→hit（matches 12 全等）→追加后 miss（新文件入结果，旧命中全保留）
    #       大范围(2101文件) bypass：identity 63ms→0ms（os.walk 禁用仍成功=决策复用），两次均真实搜索
    #       全局搜索 identity 0ms 结构旁路；invalidate 后 epoch 0→1 全部 miss
rg 三连跑确定性验证（.codex-pytest-tmp/phase28/s4/rgcheck/，2101 文件）：
    rg --line-number --column --no-heading --color=never --no-ignore-parent --no-ignore-global \
       --no-config --fixed-strings -- "needle_marker_s4" many   # ×3
    # run1 vs run2/run3: DIFFERENT（各 2101 行）；-j1 两次：SAME → 多线程序导致截断子集波动
./.venv/Scripts/python.exe -m pytest \
    tests/unit/repo/test_index.py::test_scan_does_not_evict_every_initial_cache_hit \
    tests/unit/test_phase26_closeout.py::test_global_search_bypasses_identity_scan \
    tests/unit/test_phase26_closeout.py::test_expensive_scope_is_not_rescanned \
    tests/unit/test_phase26_closeout.py::test_cache_byte_bound_and_deletion \
    tests/unit/repo/test_resolution_cache.py -q
    # 6 passed in 5.41s
```

## 8. 只读合规证据

`git status --porcelain`（本轮结束时刻；其余未跟踪文件属并行任务，` M src/codepilot/repo/index.py` 是任务简报中预先授权的构造参数改动，diff 仅 index.py:82-83/89-92/232-233 的 128/8MiB 参数化）：

```
 M src/codepilot/repo/index.py
?? （大量并行任务遗留未跟踪文件：a4/a9-a21-*.txt、logs/、scripts/phase28_*.py、
    src/codepilot/archive.py、tests/unit/test_phase28_*.py、docs/notes/phase28-*.md 等）
```

- tracked 修改数：1（即上述预授权 diff，非本任务写入；本任务零 tracked 写入）。
- 本任务唯一新文件：`docs/notes/phase28-cache-search.md`（本报告）；实验脚本与 rgcheck 均在 `.codex-pytest-tmp/`（`git check-ignore` 确认被忽略，不提交）。
- 未 commit/push，未删除任何文件。
