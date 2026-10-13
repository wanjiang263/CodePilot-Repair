"""Actual-workspace Git patch validation (P-15).

Phase 6：交付的补丁由 :func:`generate_git_diff` 生成，而它默认剔除
``tests/_codepilot_repro/``（D-06、D-07）。因此复现测试既不进入修改文件数与
行数统计，也不会被误报成非法补丁；已有测试文件仍然受到完全保护。剔除掉的条目
会作为 evidence 记录，让"补丁里为什么少了一个文件"是一个可以回答的问题。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import PurePosixPath

from codepilot.patch.cheat_detector import (
    CheatDetectionError,
    detect_ast_cheats,
    detect_hardcoded_expectations,
)
from codepilot.patch.diff import CanonicalDiff, generate_git_diff
from codepilot.repo.gitcmd import run_git
from codepilot.repo.index import RepositoryIndex
from codepilot.schemas.enums import RiskLevel
from codepilot.schemas.impact import ImpactAnalysis
from codepilot.schemas.validation import CheckName, ValidationCheck, ValidationResult
from codepilot.tools.base import WorkspacePathPolicy

__all__ = ["PatchValidator", "ValidatorConfig"]


@dataclass(frozen=True, slots=True)
class ValidatorConfig:
    max_files_changed: int = 5
    max_lines_changed: int = 150
    allow_test_edits: bool = False
    protected_files: frozenset[str] = frozenset(
        {"conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml"}
    )
    protected_directories: frozenset[str] = frozenset({".git", ".github"})


class PatchValidator:
    def __init__(
        self,
        policy: WorkspacePathPolicy,
        base_commit: str,
        *,
        index: RepositoryIndex | None = None,
        config: ValidatorConfig | None = None,
    ) -> None:
        self.policy = policy
        self.base_commit = base_commit
        self.index = index
        self.config = config or ValidatorConfig()
        self._indexed_tests = (
            {file.path for file in index.files() if file.is_test} if index is not None else set()
        )

    def validate(
        self,
        *,
        related_tests: list[str] | None = None,
        impact: ImpactAnalysis | None = None,
    ) -> ValidationResult:
        patch = generate_git_diff(self.policy.root, self.base_commit, policy=self.policy)
        checks: list[ValidationCheck] = []
        errors: list[str] = []
        warnings: list[str] = []
        findings = []
        evidence: list[str] = []

        path_errors = self._path_errors(patch)
        checks.append(_check(CheckName.PATH, path_errors))
        errors.extend(path_errors)

        size_errors: list[str] = []
        if len(patch.files) > self.config.max_files_changed:
            size_errors.append(
                f"changed files {len(patch.files)} exceed limit {self.config.max_files_changed}"
            )
        if patch.lines_added + patch.lines_deleted > self.config.max_lines_changed:
            size_errors.append(
                f"changed lines {patch.lines_added + patch.lines_deleted} exceed limit "
                f"{self.config.max_lines_changed}"
            )
        checks.append(_check(CheckName.SIZE, size_errors))
        errors.extend(size_errors)

        nonempty_errors = _nonempty_errors(patch.text)
        checks.append(_check(CheckName.NON_EMPTY, nonempty_errors))
        errors.extend(nonempty_errors)

        test_errors = self._test_protection_errors(patch.files)
        checks.append(_check(CheckName.TEST_PROTECTION, test_errors))
        errors.extend(test_errors)

        syntax_errors: list[str] = []
        sources: dict[str, tuple[str, str]] = {}
        signature_changed: list[str] = []
        for relative in patch.files:
            if not relative.endswith(".py"):
                continue
            before = self._base_text(relative)
            target = self.policy.root / relative
            after = target.read_text(encoding="utf-8") if target.exists() else ""
            sources[relative] = (before, after)
            try:
                ast.parse(after, filename=relative)
            except SyntaxError as exc:
                syntax_errors.append(f"{relative}:{exc.lineno}: {exc.msg}")
                continue
            signature_changed.extend(_changed_signatures(before, after, relative))
        checks.append(_check(CheckName.SYNTAX, syntax_errors))
        errors.extend(syntax_errors)

        cheat_errors: list[str] = []
        test_sources = self._related_test_sources(related_tests or [])
        for relative, (before, after) in sources.items():
            try:
                detected = detect_ast_cheats(before, after, relative)
                if not _is_test_path(relative, self._indexed_tests):
                    detected.extend(
                        detect_hardcoded_expectations(before, after, relative, test_sources)
                    )
            except CheatDetectionError as exc:
                cheat_errors.append(str(exc))
                continue
            findings.extend(detected)
        for finding in findings:
            location = f"{finding.file}:{finding.line or 1}"
            message = f"{finding.rule_id} {location}: {finding.detail}"
            if finding.reject:
                cheat_errors.append(message)
            else:
                warnings.append(message)
        checks.append(_check(CheckName.CHEAT, cheat_errors))
        errors.extend(cheat_errors)

        risk = impact.risk if impact is not None else RiskLevel.LOW
        if patch.excluded_files:
            evidence.append(
                "reproduction files excluded from the delivered patch: "
                + ", ".join(patch.excluded_files)
            )
        if signature_changed:
            risk = RiskLevel.HIGH
            evidence.extend(f"signature changed: {item}" for item in sorted(signature_changed))
        if impact is not None:
            evidence.extend(impact.risk_reasons)
        return ValidationResult(
            passed=not errors,
            checks=checks,
            cheat_findings=findings,
            warnings=warnings,
            errors=errors,
            files_changed=len(patch.files),
            lines_added=patch.lines_added,
            lines_deleted=patch.lines_deleted,
            risk=risk,
            evidence=evidence,
        )

    def _path_errors(self, patch: CanonicalDiff) -> list[str]:
        errors: list[str] = []
        for relative in patch.files:
            pure = PurePosixPath(relative)
            if pure.name in self.config.protected_files or any(
                directory in pure.parts for directory in self.config.protected_directories
            ):
                errors.append(f"protected path modified: {relative}")
                continue
            try:
                target = self.policy.root / relative
                self.policy.resolve(relative, must_exist=target.exists(), for_write=True)
            except ValueError as exc:
                errors.append(f"unsafe path {relative}: {exc}")
        return errors

    def _test_protection_errors(self, files: list[str]) -> list[str]:
        if self.config.allow_test_edits:
            return []
        return [
            f"existing test file modification is not allowed: {relative}"
            for relative in files
            if _is_test_path(relative, self._indexed_tests) and self._base_exists(relative)
        ]

    def _base_text(self, relative: str) -> str:
        result = run_git(
            ["show", f"{self.base_commit}:{relative}"],
            cwd=self.policy.root,
            check=False,
        )
        return result.stdout if result.returncode == 0 else ""

    def _base_exists(self, relative: str) -> bool:
        return (
            run_git(
                ["cat-file", "-e", f"{self.base_commit}:{relative}"],
                cwd=self.policy.root,
                check=False,
            ).returncode
            == 0
        )

    def _related_test_sources(self, tests: list[str]) -> dict[str, str]:
        sources: dict[str, str] = {}
        for target in tests:
            relative = target.split("::", 1)[0]
            try:
                path = self.policy.resolve(relative, must_exist=True)
            except ValueError:
                continue
            if _is_test_path(relative, self._indexed_tests):
                sources[relative] = path.read_text(encoding="utf-8")
        return sources


def _check(name: CheckName, errors: list[str]) -> ValidationCheck:
    return ValidationCheck(name=name, passed=not errors, reason="; ".join(errors) or None)


def _nonempty_errors(diff: str) -> list[str]:
    changed = [
        line[1:].strip()
        for line in diff.splitlines()
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    ]
    meaningful = [line for line in changed if line and not line.startswith("#")]
    if not changed:
        return ["patch is empty"]
    if not meaningful:
        return ["patch only changes whitespace or comments"]
    return []


def _is_test_path(relative: str, indexed_tests: set[str]) -> bool:
    path = PurePosixPath(relative)
    return (
        relative in indexed_tests
        or "tests" in path.parts
        or path.name.startswith("test_")
        or path.name.endswith("_test.py")
    )


def _changed_signatures(before: str, after: str, path: str) -> list[str]:
    try:
        old = ast.parse(before, filename=path)
        new = ast.parse(after, filename=path)
    except SyntaxError:
        return []
    old_signatures = _signatures(old)
    new_signatures = _signatures(new)
    return [
        name
        for name in sorted(old_signatures.keys() & new_signatures.keys())
        if old_signatures[name] != new_signatures[name]
    ]


def _signatures(tree: ast.AST) -> dict[str, str]:
    return {
        node.name: ast.dump(node.args, include_attributes=False)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
