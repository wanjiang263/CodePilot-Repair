# Phase 2 V-21～V-25 本地证据

开发基线：`9688388`，本地分支 `codex/phase2-retrieval-eval`。各 `*-red` 与
`*-green` 日志 / JSON / JUnit 文件保存真实命令、退出码和测试结果。

V-21：首次收集因尚无 `retrieval/eval.py` 失败；实现后 11 passed。
指标为任务级比例，缺失预测、失败任务及空 Gold 均保留在分母；空任务集返回 0。
Function 以 `(file, qualified_name)` 匹配，模块级修改用模块 Symbol。

其余任务与整阶段验收尚待执行，最终记录将在实际运行后补全。
