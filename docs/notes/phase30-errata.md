# Phase30 口径勘误

历史候选、receipt 和历史账本保留原字节；本文件补充报告口径。

- 归档相关测试合计应为 29，原文写 30。
- harness fail-closed 测试为 5，原文写 6。
- v4 的 96 个文件是 91 个继承文件加 5 个新增文件；11 是本轮指定路径数，含与继承集的重叠，不能写 91+11=96。
- 提交数存在 6/8 两种口径，未统一是否计算后续修正/文档提交。Phase31 以实际 git 哈希列表为准，不沿用该计数。
- Phase30 mypy 实际检查范围为 9 个文件，不能称全仓通过。
- 性能测量方式不同：Phase29 子进程隔离，Phase30 进程内预热与测量，不作绝对耗时同比。
- phase30-candidate-v4 的实际 parent 是 phase29-candidate-v3；本轮任务起点表中的 phase30-candidate-v3 名称有误。
- v3 标为 superseded / legacy_non_strict，未改其任何文件；v4 保持 pending，无 active 指针。
- v4 receipt 未含 manifest_commit，只能证明 current_worktree manifest 字节匹配，不能宣称 manifest 已绑定提交。
