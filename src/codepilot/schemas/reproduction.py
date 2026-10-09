"""复现结果（02 §10.5）。

复现测试是 Agent 自用的"目标测试"：SWE-bench 的判分测试在推理阶段不可见，
所以必须自己造一个能复现问题的测试，否则"修好了"没有可验证的判据。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

__all__ = ["ReproductionResult", "ReproductionStatus"]


class ReproductionStatus(StrEnum):
    REPRODUCED = "reproduced"
    """复现测试在 base commit 上按预期失败。"""

    FAILED = "failed"
    """尝试若干次仍未拿到有效复现，流程继续但只能依赖已有测试。"""

    SKIPPED = "skipped"
    """未尝试复现（例如 Issue 描述里没有可验证的行为）。"""


class ReproductionResult(BaseModel):
    status: ReproductionStatus

    test_path: str | None = None
    """复现测试文件路径，形如 ``tests/_codepilot_repro/test_repro.py``。

    该文件只用于 Agent 内部判断，最终 patch 中会被剔除（02 §10.5）。
    """

    fail_reason: str | None = None
    """base commit 上的失败信息。用于确认失败原因与 Issue 描述相关。"""

    attempts: int = 0
