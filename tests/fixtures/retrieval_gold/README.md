# 检索开发集的 Gold 来源

12 个人工编写的 synthetic-development 样例，覆盖边界条件、校验、异常、
认证、跨文件修改与模块级配置。它们基于现有 mini_auth 源码提出合成行为需求，
不代表真实用户 Bug 或正式性能基准。Issue 描述现象与期望行为，不给出补丁内容。

每条 JSONL 记录关联这里的统一 diff、补丁 SHA-256、原源码的 LF 规范化
SHA-256，以及基准源码中的文件 / qualified name / 修改行。校验器核对 diff
上下文、修改文件集合、Symbol 行范围及摘要。插入使用基准版本插入点，替换使用
被删除的旧行；Gold location 始终是 base 坐标。

Gold 补丁和 JSONL 在 mini_auth 仓库之外。推理仅接收 RetrievalInput 的四个字段；
执行器禁止导入 dataset / eval。评估侧在检索结束后才使用标签计算指标。
`snapshot` 表示无嵌套 Git 元数据的固定目录快照，执行时在私有副本生成固定提交；
真实 Git 数据集必须填完整 base SHA，禁止随 HEAD 漂移。

这是调试 / 回归开发集，不用它调优默认权重，不据此宣称检索准确率提升。
