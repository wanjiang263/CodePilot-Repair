# Phase 2 检索评估与 CLI（V-21～V-25）

基于融合分支 `9688388` 的独立本地开发；不 Push、不操作 PR #4。

V-21 指标以所有任务数为分母，缺失预测 / 失败计未命中，空 Gold 不算 Exact 命中。
Function 目标用 `(file, qualified_name)`，模块级目标用模块 Symbol；Top-K 先去重。
V-22 提供 12 条人工 synthetic-development 样例，Gold 的参考 diff / 摘要 / base
行坐标均在仓库之外，仅由评估模块读取。开发集不作为准确率改进结论。

V-23 保持 `codepilot retrieve "query" --repo ... --channel bm25` 的原始排名与 JSON。
`--issue` 接受 UTF-8 文件或文字，默认启用 BM25 并生成融合 / 文件视图；可用
`--channel bm25,ripgrep,structural,test,history` 或 `--config` JSON 独立选择六通道。
新增 JSON 的 `execution` 保存实际完整 base SHA、快照类型和执行配置，既有
RetrievalResult 字段与服务 Schema 不变。无 Git 元数据的目录在私有副本中生成
固定提交，不初始化原目录；真实 Git 仓库使用既有 fetch 隔离与基准历史查询。

配置示例（RRF 使用 `method: "rrf"`，不能同时指定 `weights`）：

```json
{
  "channels": {"bm25": true, "ripgrep": true, "semantic": true,
               "structural": true, "test": true, "history": true},
  "method": "weighted",
  "embedding": "offline-hash",
  "seed_channel": "bm25",
  "rrf_k": 60
}
```

Semantic 默认关闭；启用时必须显式提供 `offline-hash` 模式。本轮 CLI 不自动
加载模型 / API。它用真实 NumPy 余弦检索确定性的 SHA-256 词频哈希向量，是
**非学习词法基线**，不是训练语义模型的效果，也不是测试 Fake 查表。
服务的既有真实 EmbeddingProvider 接口保持兼容。Structural 单独启用时使用
BM25 Top-20 作为依赖种子，输出只包含 Structural，并明确记录种子来源。

V-24 / V-25 和最终门禁尚待运行；阶段结论将在真实验收后补全。
