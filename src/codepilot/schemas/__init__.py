"""跨模块共享的数据契约。

依赖方向：``schemas`` 不 import 本包之外的任何 codepilot 模块。
其他所有模块都可以 import 它。这条约束由 ``tests/test_architecture.py`` 强制。
"""

from __future__ import annotations

from codepilot.schemas.budget import RepairBudget
from codepilot.schemas.common import TraceFrame
from codepilot.schemas.diagnosis import Diagnosis, Evidence, EvidenceKind
from codepilot.schemas.enums import (
    ChangeAction,
    ExecutionStatus,
    FailureCategory,
    IssueType,
    RepairStrategy,
    ReviewSeverity,
    RiskLevel,
    RunStatus,
    StopReason,
    TaskMode,
    TestOutcome,
)
from codepilot.schemas.execution import ExecutionResult, TestBaseline, TestCaseResult
from codepilot.schemas.impact import ImpactAnalysis, ImpactedSymbol, ImpactedTest, Relation
from codepilot.schemas.issue import IssueAnalysis
from codepilot.schemas.localization import LocalizationResult
from codepilot.schemas.plan import PlannedChange, RepairPlan
from codepilot.schemas.progress import ProgressSnapshot, made_progress
from codepilot.schemas.regression import (
    RegressionLevel,
    RegressionMode,
    RegressionResult,
)
from codepilot.schemas.reproduction import (
    ENVIRONMENT_FAILURE_KINDS,
    MAX_REPRO_ATTEMPTS,
    MAX_REPRO_RETRIES,
    REPRO_TEST_DIRECTORY,
    ReproductionAttempt,
    ReproductionFailureKind,
    ReproductionResult,
    ReproductionStatus,
    is_reproduction_test_path,
    validate_reproduction_test_path,
)
from codepilot.schemas.retrieval import (
    ChannelScore,
    FusionMethod,
    RankedFile,
    RankedSymbol,
    RetrievalQuery,
    RetrievalResult,
)
from codepilot.schemas.review import ReviewIssue, ReviewResult
from codepilot.schemas.state import Attempt, RepairState
from codepilot.schemas.usage import ModelUsage, Usage
from codepilot.schemas.validation import (
    CheatFinding,
    CheatPattern,
    CheckName,
    ValidationCheck,
    ValidationResult,
)

__all__ = [
    "ENVIRONMENT_FAILURE_KINDS",
    "MAX_REPRO_ATTEMPTS",
    "MAX_REPRO_RETRIES",
    "REPRO_TEST_DIRECTORY",
    "Attempt",
    "ChangeAction",
    "ChannelScore",
    "CheatFinding",
    "CheatPattern",
    "CheckName",
    "Diagnosis",
    "Evidence",
    "EvidenceKind",
    "ExecutionResult",
    "ExecutionStatus",
    "FailureCategory",
    "FusionMethod",
    "ImpactAnalysis",
    "ImpactedSymbol",
    "ImpactedTest",
    "IssueAnalysis",
    "IssueType",
    "LocalizationResult",
    "ModelUsage",
    "PlannedChange",
    "ProgressSnapshot",
    "RankedFile",
    "RankedSymbol",
    "RegressionLevel",
    "RegressionMode",
    "RegressionResult",
    "Relation",
    "RepairBudget",
    "RepairPlan",
    "RepairState",
    "RepairStrategy",
    "ReproductionAttempt",
    "ReproductionFailureKind",
    "ReproductionResult",
    "ReproductionStatus",
    "RetrievalQuery",
    "RetrievalResult",
    "ReviewIssue",
    "ReviewResult",
    "ReviewSeverity",
    "RiskLevel",
    "RunStatus",
    "StopReason",
    "TaskMode",
    "TestBaseline",
    "TestCaseResult",
    "TestOutcome",
    "TraceFrame",
    "Usage",
    "ValidationCheck",
    "ValidationResult",
    "is_reproduction_test_path",
    "made_progress",
    "validate_reproduction_test_path",
]
