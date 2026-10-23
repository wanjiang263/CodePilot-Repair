"""Run the internal M2 graph without submitting to the official harness.

The stock smoke script runs the official harness as soon as a prediction exists.
For ``l2cal10e`` the rule is the opposite: the official harness may only see the
attempt that already passed the internal closure. This wrapper injects a
deferring harness through the documented ``SmokeDeps`` seam, so the report keeps
an explicit ``NOT_EVALUATED`` verdict instead of pretending to have an official
result.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import swebench_smoke as smoke  # noqa: E402


class _DeferredExecution:
    def __init__(self, instance_id: str) -> None:
        self.run_id = None
        self.command = None
        self.decision = "DEFERRED"
        self.exit_code = None
        self.timed_out = False
        self.runtime_seconds = 0.0
        self.error_kind = None
        self.error_detail = None
        self.reused = False
        self.reused_reason = None
        self.warnings: tuple[str, ...] = ()
        self.verdicts = (
            SimpleNamespace(
                instance_id=instance_id,
                verdict="NOT_EVALUATED",
                resolved=None,
                report_path=None,
                report_sha256=None,
                error_kind=None,
                error_detail=("official harness deferred until the internal closure is accepted"),
                runtime_seconds=None,
            ),
        )
        self.report = None
        self.report_path = None
        self.report_sha256 = None
        self.included_in_resolved_rate = False


class _DeferredHarness:
    def __init__(self, instance_id: str) -> None:
        self._instance_id = instance_id

    def run(self, *, dry_run: bool = False) -> _DeferredExecution:
        if dry_run:
            return _DeferredExecution(self._instance_id)
        return _DeferredExecution(self._instance_id)


class _ReusingWorkspaceManager:
    """重新接管上一轮遗留的 workspace，而不是从缓存再克隆一份。

    ``WorkspaceManager.create`` 对非空目录一律拒绝，这条规则本身是对的：它拦住
    了把旧现场当成新现场。这里只对**显式指定**的那一个 target 放行，并重新核对
    HEAD=base、工作区干净、repository_id 一致；其它 target（例如复现基线）仍然
    走原管理器。
    """

    def __init__(self, delegate: Any, target: Path) -> None:
        self._delegate = delegate
        self._target = Path(target).resolve()

    def create(
        self,
        *,
        source: Any,
        base_commit: str,
        target: Path,
        mode: str = "fetch",
    ) -> Any:
        from codepilot.repo.gitcmd import run_git
        from codepilot.repo.workspace import (
            Workspace,
            WorkspaceError,
            normalize_source,
            repository_id,
        )

        resolved_target = Path(target).resolve()
        if resolved_target != self._target:
            return self._delegate.create(
                source=source, base_commit=base_commit, target=resolved_target, mode=mode
            )
        src = normalize_source(source) if isinstance(source, str) else source
        resolved = self._delegate.rev_parse(resolved_target, base_commit)
        if resolved is None:
            raise WorkspaceError(f"复用 workspace 里找不到 base_commit: {base_commit}")
        head = run_git(["rev-parse", "HEAD"], cwd=resolved_target).stdout.strip()
        if head != resolved:
            raise WorkspaceError(f"复用 workspace 的 HEAD 不是 base_commit: {head} != {resolved}")
        workspace = Workspace(
            path=resolved_target,
            source=src,
            base_commit=resolved,
            repository_id=repository_id(src, resolved),
        )
        workspace.assert_clean()
        return workspace

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


def _reuse_workspace_manager(config: Any) -> Any:
    target = os.environ.get("CODEPILOT_REUSE_WORKSPACE")
    if not target:
        return None
    from codepilot.repo.workspace import WorkspaceManager

    delegate = WorkspaceManager(cache_root=Path(config.artifact_root) / "repo-cache")
    return _ReusingWorkspaceManager(delegate, Path(target))


def main(argv: list[str] | None = None) -> int:
    args = smoke._parse_args(argv)
    config = smoke._config_from_args(args)
    deps = smoke.SmokeDeps(
        harness_runner=_DeferredHarness(config.instance_id),
        workspace_manager=_reuse_workspace_manager(config),
    )
    reports = smoke.run_smoke_set(config, repeats=args.repeats, deps=deps)
    payload: dict[str, Any] = {
        "model_run_status": (
            smoke.LIVE_RUN
            if any(report.get("model_run_status") == smoke.LIVE_RUN for report in reports)
            else smoke.LIVE_NOT_RUN
        ),
        "official_evaluation": "DEFERRED",
        "runs": [
            {
                "run_label": report.get("run_label"),
                "run_id": report.get("run_id"),
                "status": report.get("status"),
                "report_path": report.get("report_path"),
                "prediction": (report.get("prediction") or {}).get("status"),
                "official_verdict": "NOT_EVALUATED",
            }
            for report in reports
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
