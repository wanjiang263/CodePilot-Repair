# Phase24 执行检查点（未完成）

基线 `1d8416e9d986b93005d25ed01d856167c6128df8`。固定 catalog `artifacts/phase22/next-stage/catalog.json` SHA256 `3cfa3b13cbbf281aafb216a6942bfbdc4c249c5c082c715218ea786c2cc0c3be`，50 个唯一实例，和冻结排除集无交集。`configs/subsets/swebench-lite-test50.json` 是另一份旧子集，包含本轮调试题，不用于本次正式批次。

## 调试集

三个补丁均为隔离镜像中人工修订，人工候选 prediction 标识为 `manual-debug-phase24`，不冒充 Agent；均不属于 test50。原候选和 prediction 字节哈希逐题一致，Phase23 证据未改写。

- Sphinx：公开问题要求 section 目录；`artifacts/phase24-debug/sphinx_behavior.py` 实际构建 section 1/3，关闭开关输出 `one.1`、`three.3`，开启输出 `man1/one.1`、`man3/three.3`。新候选 `sphinx-debug.diff` 修正日志并保留旧默认。手册测试基线和候选均为 3 passed、1 failed（同一 caption 断言）；官方 `manual-debug-phase24.phase24-sphinx-debug.json` 为 UNRESOLVED，仍未确定全部未满足的产品行为。
- Matplotlib：原候选使用不同接口名且无法完整处理 PEP 440 版本。新候选 `matplotlib-debug.diff` 使用现有 packaging 解析 `version_info`；实际测试覆盖 final、rc、dev、post 和 local，原版本仍可读取。`test_matplotlib.py` 为 3 passed、1 skipped（root 下 chmod 不生效）。官方 `manual-debug-phase24.phase24-matplotlib-debug-r3.json` 为 UNRESOLVED。该候选返回 `packaging.version.Version`，其比较对象是同类型版本对象；没有证明满足所有接口要求。
- Requests：原官方输出为 74 passed、2 failed、108 errors；错误为递归 httpbin fixture，两个失败涉及网络超时。补装 pytest-httpbin 2.0.0 / httpbin 0.10.4 后，相同镜像、网络的基线与原候选各 178 passed、4 failed、1 error、1 xfailed、1 xpassed；185 个 JUnit 测试状态一致。派生镜像 ID `sha256:7b3ab30c6c7ae7d7e708bdad06256255f38eeaa26ca7b3db7e1c0ae18b743ca9`，父镜像 `sha256:c5dc4ba64a2d97d6cff6b82fe9c0561589df13f23df6883d05e99b630942bbd1`，与 Phase23 旧父镜像记录不同。原 prediction 在独立 phase24 命名空间官方复验 UNRESOLVED。流探测发现原候选在迭代器创建时消费一次性响应导致空输出。修订 `requests-debug.diff` 保留已读字节、延迟读取，相关行为探测通过，185 项既有测试状态与基线一致；新官方结果 UNRESOLVED。编码检测未能提前判定时会缓存输入，未证明大响应的内存表现。

原始 logs、JUnit、候选、prediction 和官方汇总均位于 `artifacts/phase24-debug/`；既有 Phase23 结果保持原位。首次 Matplotlib Harness 在导入可选 Modal 依赖时 OOM；限制线程及减少竞争后重试得到有效结果，旧失败保留。

## 环境及正式批次

逐题快照 `artifacts/phase24-debug/test50-status-snapshot.json` 和下载进度 `artifacts/phase24-test50-provision-r2/progress.jsonl` 是检查点；base+CLI 并非完整环境就绪。已执行的公开问题相关测试和结果见快照；宿主 workspace/index 身份及其余实例真实测试未完成。固定分母仍为 50。真实 Agent 派发、合法正式候选、官方评分均为 0/50；有效官方解决率无分母，不能计算；未运行项不是 NO_PATCH 或 UNRESOLVED。

M3 原先硬编码六实例与六仓库，并且内部拒绝直接跳过官方评分。当前修改允许多个实例共享仓库、对内部拒绝的合法候选走独立评测、使用配置模型身份，并在当前 Docker Desktop 使用 guest-services socket。该改动仍需要真实批次链路验证。M3 仍会对一些单实例基础设施错误停止全批；未知费用需要在确认进程停止后按授权建立新来源段。正式配置、共享预算、环境探测和版本冻结未完成，不应开始付费派发。

一次范围过宽的代码搜索意外显示仓库 fixture 中某 test50 实例的参考补丁片段；没有把它发送给任何 Agent，也没有据此修改该题方案或换题。操作人员已接触该实例的信息，严格盲态分析需对此实例单独标记，保持固定分母。

## 费用与验证

历史九个授权来源哈希匹配，原授权累计上界 3.19763192 CNY（包括未知预留）；余额 26.80236808 CNY。本轮模型请求 0，新增 settled 0，新增 unknown reserved 0，上界 0。未发生本轮模型路由偏差；没有实际返回模型标识可记录。历史 opening 不重复作为本轮新增费用。

相关单元测试最近一次 45 passed；相关 Ruff lint/format 通过，mypy 针对候选和批次/官方脚本通过。将所有 Phase23 历史脚本强行纳入 mypy 时发现既有未标注问题，不能当作全范围通过。尚未执行 test50 正式运行和评测、全量逐题环境验证、全部恢复路径及完整报告；不应将本检查点当成完成报告。

检查点更新：本次暂停下载后 27/50 完成 base/CLI 核验、16/50 执行过相关真实测试；完整工作区绑定验收 0/50。下载进程停止，已有层和两段进度保留，后续可从 `artifacts/phase24-test50-provision-r2` 恢复。该进度不是 test50 正式结果。
