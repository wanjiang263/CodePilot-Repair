# Phase34 统计口径勘误（C39）

本文件记录 C38 收口后发现的数字口径问题。只修文档，不改 v12 candidate /
receipt / witness，也不生成新候选。

## 实测命令与结果

在 worktree 根目录执行：

```bash
git rev-list --count HEAD
# 318

git ls-files | python -c "..."   # 见下方口径
# 787 个有效 SOURCE_SCOPE 文件

git -C D:/Projects/Desktop/CodePilot-Repair status --porcelain | wc -l
# 172（untracked status 条目）

git -C D:/Projects/Desktop/CodePilot-Repair ls-files --others --exclude-standard | wc -l
# 176（untracked 文件；logs/ 的 5 个文件在 status 中折叠为 1 条）
```

有效 SOURCE_SCOPE 口径：`src/`、`scripts/`、`tests/`、`web/src/`、
`configs/harness/`、`pyproject.toml`、`web/package.json`，并排除
`configs/harness/candidates/` 与 `configs/harness/receipts/`。当前共 787 个
tracked 文件（含 `configs/harness/.gitattributes`）。

## 133 与 787 的区别

`phase34-candidate-v12` 的 `resolved_source_files=133` 不是“全部源码文件”，
而是“父候选继承的策展 source 集合 + `diff_base..source_commit` 内所有
SOURCE_SCOPE 变更”。v9 源点 `cc0dd9a..C33` 的 7 个 scope 变更全部被
v10/v11/v12 的 `changed_files` 覆盖；787 是当前 tracked 的完整 scope 口径。
两者不是同一层概念，报告不再把 133 写成“全部源码”。

## Shallow 历史

`.git/shallow` 的边界是 `04a523fafbd61bc2e49420963b84ed8e2bd1b3cf`；该提交
不在任何 origin 分支上，`git fetch --unshallow` 退出 0 但不改变 shallow 文件。
C38 时 HEAD 的历史是 318 条。clean-clone 只覆盖这 318 条；完整历史 clone
未验证。

## P2 路径更正

原报告写的 `src/codepilot/eval/swebench_runner.py:423` 不是实际的
`images.build` 位置。实际是 `src/codepilot/sandbox/swebench_env.py:423` 的
`DockerSdkBackend.build`；官方路径不调用它（`tests/sandbox/test_swebench_env.py`
断言 `build_calls==[]`），通用 DockerSandbox 的 base build 已使用
`fileobj` 微上下文（`src/codepilot/sandbox/docker.py:354`）。本轮只做审计，
不改 `src/`；该 P2 作为“潜在误用、guard 延后”记录，不宣称已修。

## 未改动

`configs/harness/candidates/phase34-candidate-v12/` 四文件、
`configs/harness/receipts/phase34-candidate-v12.json` 与
`docs/notes/phase34-evidence/external-witness-v12.txt` 的字节未变。
本次只修正 Phase34 报告、证据包 README 与 Phase34 delivery receipt 的文字/统计，
并按证据包规则重新生成 `SHA256SUMS` 与 runtime JSON。
