"""全局枚举。

集中定义的理由：这些取值会出现在 state、数据库、事件流和报告四个地方，
分散定义迟早会出现"数据库里写 low、报告里比 LOW"这种问题。

文档中部分字段写作 ``Literal["low", "medium", "high"]``，这里统一改为
``StrEnum``。二者校验与序列化行为一致（值就是那些字符串），但 ``StrEnum``
可以被多处复用，且能直接用于字典键与日志。
"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar

__all__ = [
    "ChangeAction",
    "ExecutionStatus",
    "FailureCategory",
    "IssueType",
    "RepairStrategy",
    "ReviewSeverity",
    "RiskLevel",
    "RunStatus",
    "StopReason",
    "TaskMode",
    "TestOutcome",
]


class StopReason(StrEnum):
    """02「停止规则」表中的取值，外加 Phase 5 之后各轮实际引入的终态
    （``patch_rejected`` / ``tests_failed`` / ``tool_error`` / ``budget_tool_calls`` /
    ``workflow_error`` / ``plan_rejected``）。新增它们没有改 02 的语义，只是把
    「谁错了」拆开写：``workflow_error`` 是 Harness 的问题，``plan_rejected`` 是模型
    给出的计划不合法，两者混在一起就没法算干净的评测分母。"""

    BUDGET_ITERATIONS = "budget_iterations"
    BUDGET_TEST_RUNS = "budget_test_runs"
    BUDGET_TOKENS = "budget_tokens"
    BUDGET_COST = "budget_cost"
    BUDGET_TIME = "budget_time"
    REPEATED_PATCH = "repeated_patch"
    REPEATED_FAILURE = "repeated_failure"
    NO_PROGRESS = "no_progress"
    SANDBOX_FAILURE = "sandbox_failure"
    VALIDATION_LOOP = "validation_loop"
    DIAGNOSIS_GIVE_UP = "diagnosis_give_up"
    PATCH_REJECTED = "patch_rejected"
    TESTS_FAILED = "tests_failed"
    TOOL_ERROR = "tool_error"
    BUDGET_TOOL_CALLS = "budget_tool_calls"
    #: 审查专属额度（``max_review_calls``）耗尽。刻意与 ``budget_tool_calls`` 分开：
    #: 前者说明"审查阶段被自己的次数上限拦下"，后者说明"编码阶段没工具调用了"。
    #: 混成一个原因就看不出是 Coder 把 Reviewer 饿死了，还是审查真的轮次用完了。
    BUDGET_REVIEW_CALLS = "budget_review_calls"
    #: 剩余测试额度撑不起**下一版候选补丁的验证闭环**，于是在启动修复之前就被拦下。
    #: 闭环测试额度准入：**还没花完，但已经算出来剩余额度撑不起下一版候选的闭环**。
    #: 留在 ``budget_*`` 家族里，既有的归因分桶（``TaskOutcome.over_budget``、
    #: SWE-bench 的 ``BUDGET_STOPPED``）才会把它算成预算停止而不是"补丁无效/模型没修好"；
    #: 值本身与 ``budget_test_runs`` 区分：后者是"12 次已经花完"（运行结果），这一条是
    #: "还剩 2 次而闭环要 3 次"（派发前的算术），修法完全不同。
    BUDGET_CLOSURE_TEST_RUNS = "budget_closure_test_runs"
    WORKFLOW_ERROR = "workflow_error"
    #: 用户/服务请求的协作式取消。位于安全节点边界，已经发生的外部副作用、
    #: 已花费的费用与补丁证据全部保留；它不是成功，也不是模型失败。
    CANCELLED = "cancelled"
    #: 计划被语义/Schema 校验拒绝（纠正预算耗尽）。刻意与 ``workflow_error`` 分开：
    #: 前者是"模型没给出可执行的计划"，后者是"Harness 自己出了问题"，
    #: 混在一起会让一次基础设施崩溃被计入模型修复失败的分母。
    PLAN_REJECTED = "plan_rejected"


class RunStatus(StrEnum):
    """``RepairState.status`` 的取值（02「RepairState」）。"""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STOPPED = "stopped"
    ERROR = "error"
    PROVISIONALLY_VERIFIED = "provisionally_verified"
    PATCH_REJECTED = "patch_rejected"
    PLAN_REJECTED = "plan_rejected"
    TESTS_FAILED = "tests_failed"
    SANDBOX_ERROR = "sandbox_error"
    TOOL_ERROR = "tool_error"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CANCELLED = "cancelled"


class TaskMode(StrEnum):
    """任务来源。决定信息可见范围与评测方式。"""

    SYNTHETIC = "synthetic"
    SWEBENCH = "swebench"
    USER = "user"


class ExecutionStatus(StrEnum):
    """02「ExecutionResult / 状态判定」表的全部取值。"""

    PASS = "PASS"
    FAIL = "FAIL"
    TIMEOUT = "TIMEOUT"
    COLLECTION_ERROR = "COLLECTION_ERROR"
    ENV_ERROR = "ENV_ERROR"
    SANDBOX_ERROR = "SANDBOX_ERROR"


class TestOutcome(StrEnum):
    """单个测试用例的结果（对应 junit XML 的四种取值）。"""

    # 名字以 Test 开头但它是枚举不是测试类；pytest 会误收集。
    __test__: ClassVar[bool] = False

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"


class FailureCategory(StrEnum):
    """02「Failure Classifier」的 12 个类别。"""

    SYNTAX = "syntax"
    IMPORT = "import"
    NAME_ATTRIBUTE = "name_attribute"
    TYPE = "type"
    VALUE_KEY_INDEX = "value_key_index"
    ASSERTION = "assertion"
    EXPECTED_EXCEPTION_MISSING = "expected_exception_missing"
    TIMEOUT = "timeout"
    COLLECTION = "collection"
    FIXTURE = "fixture"
    ENVIRONMENT = "environment"
    UNKNOWN = "unknown"


class IssueType(StrEnum):
    """``IssueAnalysis.issue_type``。"""

    BUG = "bug"
    FEATURE = "feature"
    REGRESSION = "regression"
    PERFORMANCE = "performance"
    OTHER = "other"


class RiskLevel(StrEnum):
    """Impact Analyzer 的风险分级，同时用于 ``RepairPlan.risk`` 与 ``ReviewResult.risk``。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RepairStrategy(StrEnum):
    """02「修复策略」表。``give_up`` 会直接触发 ``diagnosis_give_up`` 停止。"""

    REFINE = "refine"
    REVERT_AND_RETRY = "revert_and_retry"
    EXPAND_SCOPE = "expand_scope"
    GIVE_UP = "give_up"


class ChangeAction(StrEnum):
    """``PlannedChange.action``。"""

    MODIFY = "modify"
    ADD = "add"
    DELETE = "delete"


class ReviewSeverity(StrEnum):
    """``ReviewIssue.severity``。

    只有 ``HIGH`` 会触发一次额外修复（02「Patch Reviewer」），
    因此它是控制流程的字段，不能随意扩展取值。
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
