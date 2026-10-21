"""Task adapter handed to the evaluation assembly.

It exposes only the five-field inference view (identity, issue, base commit,
repository and environment spec). No evaluation-only fields exist here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from codepilot.sandbox.env_spec import EnvSpec

__all__ = ["WebInferenceTask"]


@dataclass(frozen=True, slots=True)
class WebInferenceTask:
    task_id: str
    issue: str
    base_commit: str
    repository: Path | str
    env_spec: EnvSpec
    mode: str = "user"
