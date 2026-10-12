"""Sandbox 可注入接口与环境构建结果。"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from codepilot.sandbox.env_spec import EnvSpec
from codepilot.schemas.enums import ExecutionStatus
from codepilot.schemas.execution import ExecutionResult

__all__ = ["EnvironmentBuildResult", "Sandbox"]


class EnvironmentBuildResult(BaseModel):
    """环境镜像构建结果。

    构建失败也使用结构化结果返回，调用方因此可以区分仓库依赖问题和 Docker
    基础设施问题，而不必从异常字符串猜测。
    """

    model_config = ConfigDict(extra="forbid")

    status: ExecutionStatus
    image_ref: str | None = None
    image_id: str | None = None
    cache_key: str
    from_cache: bool = False
    duration_seconds: float = 0.0
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status is ExecutionStatus.PASS and self.image_ref is not None


@runtime_checkable
class Sandbox(Protocol):
    """测试沙箱边界；图节点只依赖此协议，不依赖 Docker 全局对象。"""

    def build_env(
        self,
        *,
        repository: str,
        base_commit: str,
        workspace: Path,
        env_spec: EnvSpec,
        allow_network: bool = False,
    ) -> EnvironmentBuildResult: ...

    def run_tests(
        self,
        *,
        workspace: Path,
        environment: EnvironmentBuildResult,
        env_spec: EnvSpec,
        tests: Sequence[str] = (),
        artifact_iteration: int = 1,
    ) -> ExecutionResult: ...

    def cleanup(self, *, older_than_seconds: int = 3600) -> list[str]: ...
