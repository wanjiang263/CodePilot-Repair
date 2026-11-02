# Phase31 plan（最终状态）

起点：a97620d3380d946abe5bab90656fbadf5db1c7a8；tracked clean。
未跟踪历史文件保留，清单见本地 `.codex-pytest-tmp/phase31/start.json`。
v4 pending，source_commit=8e1f1f47467427b97aa9e02c076e803500d9f102，diff_base=b17019beb817b92b1fbb3da879774a231d69219d。
v4 实际 parent=phase29-candidate-v3（任务起点表的 phase30-candidate-v3 名称有误）；历史文件不修改。
v5 parent=phase30-candidate-v4；diff_base=8e1f1f4；零真实模型、零新增费用；不 push。

提交链（本地，未 push）：`<S>` e56ee0b（源码修复+测试）→ `<C>` 63b8fdb（候选 v5）→ `<M>` a938429（历史 manifest）→ 26472c3（receipt）→ 最终文档提交。
外部 witness（仓库外）：`.codex-pytest-tmp/phase31/witness.txt` = d2689f7afcfee18d89c362eb81cfdc87a66d464c187b9dcbed6bcc1fee0595d8。

| Task | State | Evidence |
|---|---|---|
| T01 schema v5 | completed | src/codepilot/archive.py（candidate_schema_version 5：full manifest 边界、strict parent integrity、禁止 --source-list）；tests/unit/test_phase31_archive_manifest.py；候选 63b8fdb 四文件 |
| T02 verifier/receipt | completed | scripts/phase28_archive_verify.py v5 检查 + legacy_non_strict 分类；tests/unit/test_phase31_archive_verify.py；verify --all exit 0（witness=verified, historical_integrity=ok, new_failures=0, unexplained=0） |
| T03 receipt generator | completed | scripts/phase31_receipt.py（确定性输出、提交身份预校验）；tests/unit/test_phase31_receipt.py；configs/harness/receipts/phase31-candidate-v5.json @ 26472c3 |
| T04 16 failures | completed | 16/16 fixed（逐条：docs/notes/phase31-known-failures.json，首轮 traceback + 修复依据 + 复跑证据）；graph 全量 0 failed |
| T05 regressions | completed | phase31-runtime-results.json full_regressions：graph 266、unit 3055、integration 126、api 113、archive 143（单次整组），全部 0 failed；首轮失败证据保留 |
| T06 static checks | completed | phase31-static-checks.json：21 文件 AST/ruff check/ruff format/mypy(4 个 src+scripts)/CRLF-aware/凭据扫描 1355 行 0 findings；web/src 条件跳过 |
| T07 candidate | completed | configs/harness/candidates/phase31-candidate-v5/（4 文件，status=pending，无 active 指针）；tree cd58026d…；manifest a938429 收录 44 条 |
| T08 documentation/commits | completed | docs/notes/phase31-final-report.md + 本表 + runtime/static/known-failures 更新；5 个本地提交 |

R1 追加项（本轮完成）：test_phase9_regression.py `lf_base`、test_synthetic_prompt_isolation.py lambda、
test_phase9_docker.py `base_repo` 全部遵循 `include_tests` 关键字契约；test_phase9_resume.py 四处
`read_text` 显式 `encoding="utf-8"`（禁止仅设 PYTHONUTF8=1）。验收命令 8 passed，日志 r1.log。

首轮隔离启动失败原因：相对测试路径在隔离 cwd 下不可见，已改绝对路径；
默认 pytest temp 目录权限不足，改本轮独立 basetemp/TEMP，不修改第三方库。

整文件回归：closure-files.log 82 passed；new-archive-contract.log 41 passed；archive-compat-final 27 passed；
archive-late-guards 19 passed（以上为过程中证据，最终以 full_regressions 的单命令整跑为准）。
静态：21 个 Python 文件 AST/ruff check/ruff format；mypy 仅 4 个 src/scripts 文件。
CRLF 原样保留（原 CRLF 文件仍 CRLF），默认 git diff --check 的 CR 告警另记，CRLF-aware check exit 0；diff 凭据扫描 1355 行 0 findings。
web/src 自 8e1f1f4 起 0 改动 → 前端条件跳过，未伪称重跑。
