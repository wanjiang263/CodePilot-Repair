"""Per-instance native test-runner adapters.

The official SWE-bench images are not all pytest projects.  Django's
``tests/runtests.py`` and SymPy's ``bin/test`` do not understand pytest's
``--junitxml`` flag, and their output formats are repository-specific.  Treating
them as pytest produced two different failures in Phase 20: a hard collection
error (Django) and a runner that silently executed zero tests (SymPy).

This module keeps that difference typed:

* the frozen runner configuration decides which adapter an instance uses;
* the adapter turns canonical workspace paths/test node ids into the native
  selectors the runner accepts;
* the adapter parses the native output into the same ``ExecutionResult`` shape
  every other consumer already understands;
* the raw stdout/stderr remain artifacts, and the parse summary records the
  runner type, the command, parsed/wanted counts and any ambiguity.

Canonical node ids stay path-based (``tests/foo/test_bar.py::Class::test_x``)
so the existing workspace path policy and patch-safety checks keep working.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from codepilot.schemas.enums import ExecutionStatus, TestOutcome
from codepilot.schemas.execution import TestCaseResult

__all__ = [
    "DJANGO",
    "PYTEST",
    "PYTEST_REQUESTS_COMPAT",
    "SYMPY",
    "NativeParse",
    "RunnerAdapter",
    "RunnerConfig",
    "RunnerOverrides",
    "adapter_for",
    "django_node",
    "django_selector",
    "requested_filter",
    "sympy_selector",
]

PYTEST = "pytest"
PYTEST_REQUESTS_COMPAT = "pytest_requests_compat"
DJANGO = "django"
SYMPY = "sympy"

_SUPPORTED = {PYTEST, PYTEST_REQUESTS_COMPAT, DJANGO, SYMPY}
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_DJANGO_STATUS = re.compile(
    r"^(?P<name>test\S+) \((?P<module>[^)]+)\)\s*(?:\.\.\.\s*(?P<status>.*))?$"
)
_DJANGO_BLOCK = re.compile(r"^(?P<kind>FAIL|ERROR): (?P<name>\S+) \((?P<module>[^)]+)\)$")
_DJANGO_RAN = re.compile(r"^Ran (?P<count>\d+) tests? in ")
_DJANGO_SUMMARY = re.compile(r"^(?:OK|FAILED) \((?P<body>.*)\)$")
_SYMPY_FILE = re.compile(r"^(?P<path>\S+\.py)\[(?P<count>\d+)\]$")
_SYMPY_TEST = re.compile(r"^(?P<name>test_\w+)[ \t]+(?P<status>ok|F|E|f|X|T|s)$")
_SYMPY_TEST_REASON = re.compile(r"^(?P<name>test_\w+)[ \t]+(?P<reason>.*?)[ \t]+s$")
_SYMPY_BLOCK = re.compile(r"^_{3,} (?P<path>\S+\.py):(?P<name>test_\w+) _{3,}$")
_SYMPY_SUMMARY = re.compile(r"tests finished:\s*(?P<body>.*?)\s+in\s+[\d.]+\s+seconds", re.DOTALL)


class RunnerConfig(BaseModel):
    """Frozen runner decision for one instance."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str
    test_command: str
    preflight_scope: tuple[str, ...] = ()
    timeout_seconds: int | None = Field(default=None, ge=1, le=3600)
    note: str = ""

    def validate_kind(self) -> None:
        if self.kind not in _SUPPORTED:
            raise ValueError(f"unsupported runner kind: {self.kind}")
        if not self.test_command.strip():
            raise ValueError("runner test_command must not be empty")


class RunnerOverrides:
    """Typed, hashable view of ``instance-runner-overrides.json``."""

    def __init__(self, payload: dict[str, Any], *, path: Path | None = None) -> None:
        if payload.get("schema_version") != 1:
            raise ValueError("runner overrides schema_version must be 1")
        instances = payload.get("instances")
        if not isinstance(instances, dict) or not instances:
            raise ValueError("runner overrides must contain a non-empty instances object")
        self.instances: dict[str, RunnerConfig] = {}
        for instance_id, raw in instances.items():
            if not isinstance(raw, dict):
                raise ValueError(f"runner override for {instance_id} must be an object")
            config = RunnerConfig.model_validate(raw)
            config.validate_kind()
            self.instances[str(instance_id)] = config
        self.path = path
        self.sha256 = hashlib.sha256(path.read_bytes()).hexdigest() if path is not None else None

    @classmethod
    def load(cls, path: Path | str | None) -> RunnerOverrides | None:
        if path is None:
            return None
        resolved = Path(path)
        payload = json.loads(resolved.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("runner overrides must be a JSON object")
        return cls(payload, path=resolved)

    def for_instance(self, instance_id: str) -> RunnerConfig | None:
        return self.instances.get(instance_id)

    def kinds(self) -> dict[str, str]:
        return {name: config.kind for name, config in sorted(self.instances.items())}


@dataclass(frozen=True, slots=True)
class NativeParse:
    tests: list[TestCaseResult]
    status: ExecutionStatus
    error_reason: str | None
    diagnostics: dict[str, Any] = field(default_factory=dict)
    requested: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()


def _node_join(path: str, parts: list[str]) -> str:
    return path + ("::" + "::".join(parts) if parts else "")


def _existing(path: Path) -> bool:
    return path.is_file()


def django_node(module: str, class_path: list[str], test_name: str, workspace: Path) -> str:
    """Map Django's ``(app.TestClass)`` display to a canonical path NodeID."""
    cleaned = module.strip(".")
    if cleaned.startswith("tests."):
        cleaned = cleaned[len("tests.") :]
    parts = [part for part in cleaned.split(".") if part]
    for index in range(len(parts), 0, -1):
        candidate = "tests/" + "/".join(parts[:index]) + ".py"
        if _existing(workspace / candidate):
            return _node_join(candidate, [*parts[index:], *class_path, test_name])
    # Keep the native label visible instead of inventing a path that does not exist.
    native = ".".join([*parts, *class_path, test_name])
    return f"<native>:{native}"


def _path_to_module(path: str) -> str:
    normalized = path.replace("\\", "/")
    if normalized.startswith("tests/"):
        normalized = normalized[len("tests/") :]
    if normalized.endswith(".py"):
        normalized = normalized[: -len(".py")]
    return normalized.replace("/", ".")


def django_selector(node: str, workspace: Path) -> str:
    """Convert a canonical path NodeID (or module label) to a Django test label."""
    del workspace  # the conversion is purely textual; kept for a uniform adapter API
    if "::" in node:
        path, _, rest = node.partition("::")
        parts = [part for part in rest.split("::") if part]
        return ".".join([_path_to_module(path), *parts])
    normalized = node.replace("\\", "/").strip()
    if normalized.endswith(".py") or "/" in normalized:
        return _path_to_module(normalized)
    return normalized


def sympy_selector(node: str) -> str:
    """SymPy's runner receives file paths; node-level precision is applied after parse."""
    path = node.split("::", 1)[0].replace("\\", "/")
    if path.startswith("./"):
        path = path[2:]
    return path


def requested_filter(
    tests: list[TestCaseResult], requested: tuple[str, ...]
) -> tuple[list[TestCaseResult], tuple[str, ...], list[str]]:
    """Keep requested nodes, report missing ones and unrelated observed nodes."""
    if not requested:
        return tests, (), []
    wanted = set(requested)
    kept = [test for test in tests if test.node_id in wanted]
    present = {test.node_id for test in kept}
    missing = tuple(sorted(wanted - present))
    extra = sorted({test.node_id for test in tests} - wanted)
    return kept, missing, extra


def _case(node: str, outcome: TestOutcome, message: str | None = None) -> TestCaseResult:
    return TestCaseResult(node_id=node, outcome=outcome, duration=0.0, message=message)


def _summary_counts(body: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in body.split(","):
        name, _, value = item.strip().partition("=")
        try:
            counts[name.strip()] = int(value)
        except ValueError:
            continue
    return counts


def _decode(raw: bytes | str) -> str:
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    return _ANSI.sub("", text)


def parse_django(
    stdout: str | bytes,
    stderr: str | bytes,
    *,
    exit_code: int | None,
    workspace: Path,
    requested: tuple[str, ...] = (),
    timed_out: bool = False,
) -> NativeParse:
    """Parse ``tests/runtests.py --verbosity 2`` output."""
    text = _decode(stdout) + "\n" + _decode(stderr)
    lines = [line.strip() for line in text.splitlines()]
    outcomes: dict[str, TestOutcome] = {}
    pending: tuple[str, list[str], str] | None = None
    ran = None

    def record(module: str, class_path: list[str], name: str, outcome: TestOutcome) -> None:
        node = django_node(module, class_path, name, workspace)
        outcomes[node] = outcome

    def finish(status_text: str) -> None:
        nonlocal pending
        if pending is None:
            return
        module, class_path, name = pending
        lowered = status_text.lower()
        if lowered.startswith("ok"):
            record(module, class_path, name, TestOutcome.PASSED)
        elif lowered.startswith("skipped"):
            record(module, class_path, name, TestOutcome.SKIPPED)
        elif lowered.startswith("fail"):
            record(module, class_path, name, TestOutcome.FAILED)
        elif lowered.startswith("error"):
            record(module, class_path, name, TestOutcome.ERROR)
        pending = None

    for line in lines:
        if not line:
            continue
        ran_match = _DJANGO_RAN.match(line)
        if ran_match:
            ran = int(ran_match.group("count"))
            continue
        block = _DJANGO_BLOCK.match(line)
        if block:
            module = block.group("module")
            class_parts: list[str] = []
            # ``FAIL: test (module.Class)``: the class is embedded in the display.
            bits = module.split(".")
            path_parts = bits
            for index in range(len(bits), 0, -1):
                candidate = "tests/" + "/".join(bits[:index]) + ".py"
                if _existing(workspace / candidate):
                    path_parts = bits[:index]
                    class_parts = bits[index:]
                    break
            node = django_node(".".join(path_parts), class_parts, block.group("name"), workspace)
            outcomes[node] = (
                TestOutcome.FAILED if block.group("kind") == "FAIL" else TestOutcome.ERROR
            )
            continue
        match = _DJANGO_STATUS.match(line)
        if match:
            finish("")  # a new test started; a missing status stays unrecorded
            module = match.group("module")
            bits = module.split(".")
            class_parts = []
            for index in range(len(bits), 0, -1):
                candidate = "tests/" + "/".join(bits[:index]) + ".py"
                if _existing(workspace / candidate):
                    class_parts = bits[index:]
                    module = ".".join(bits[:index])
                    break
            pending = (module, class_parts, match.group("name"))
            status = match.group("status")
            if status:
                finish(status)
            continue
        if pending is not None:
            lowered = line.lower()
            if lowered == "ok":
                finish("ok")
            elif lowered.startswith("skipped"):
                finish("skipped " + lowered)
            elif lowered.startswith("... ok") or lowered.endswith(" ... ok"):
                finish("ok")
            elif lowered.startswith("... fail") or lowered.endswith(" ... fail"):
                finish("FAIL")
            elif lowered.startswith("... error") or lowered.endswith(" ... error"):
                finish("ERROR")
    finish("")  # trailing pending test without a status is intentionally unrecorded

    tests = [_case(node, outcome) for node, outcome in sorted(outcomes.items())]
    kept, missing, extra = requested_filter(tests, requested)
    counts = {
        "ran": ran,
        "parsed": len(tests),
        "wanted": len(requested) or None,
        "missing": len(missing),
        "extra_observed": len(extra),
    }
    diagnostics: dict[str, Any] = {
        "runner": DJANGO,
        "parse_counts": counts,
        "requested_missing": list(missing),
        "requested_extra_observed": extra[:20],
    }
    if not tests:
        return NativeParse([], ExecutionStatus.COLLECTION_ERROR, "NO_TESTS_COLLECTED", diagnostics)
    if missing:
        return NativeParse(
            kept, ExecutionStatus.COLLECTION_ERROR, "REQUESTED_TESTS_NOT_COLLECTED", diagnostics
        )
    failing = [test for test in kept if test.outcome in {TestOutcome.FAILED, TestOutcome.ERROR}]
    if timed_out:
        return NativeParse(kept, ExecutionStatus.TIMEOUT, "EXECUTION_TIMEOUT", diagnostics)
    if exit_code in {2, 4, 5}:
        return NativeParse(
            kept, ExecutionStatus.COLLECTION_ERROR, "NATIVE_COLLECTION_ERROR", diagnostics
        )
    if exit_code == 0 and failing:
        return NativeParse(
            kept, ExecutionStatus.FAIL, "EXIT_CODE_OUTCOME_CONTRADICTION", diagnostics
        )
    if exit_code == 0:
        return NativeParse(kept, ExecutionStatus.PASS, None, diagnostics)
    if exit_code == 1:
        reasons = [test.node_id for test in failing][:10]
        diagnostics["failing_nodes"] = reasons
        return NativeParse(kept, ExecutionStatus.FAIL, None, diagnostics)
    diagnostics["exit_code"] = exit_code
    return NativeParse(
        kept, ExecutionStatus.SANDBOX_ERROR, "UNEXPECTED_CONTAINER_EXIT", diagnostics
    )


def parse_sympy(
    stdout: str | bytes,
    stderr: str | bytes,
    *,
    exit_code: int | None,
    workspace: Path,
    requested: tuple[str, ...] = (),
    timed_out: bool = False,
) -> NativeParse:
    """Parse ``bin/test -C --verbose`` output (file headers + per-test status chars)."""
    del workspace  # SymPy paths are already repository-relative.
    text = _decode(stdout) + "\n" + _decode(stderr)
    active_file: str | None = None
    outcomes: dict[str, TestOutcome] = {}
    blocks: dict[str, str] = {}
    file_counts: dict[str, int] = {}
    summary: dict[str, int] = {}
    pending: tuple[str, str] | None = None

    def record(path: str, name: str, status: str, reason: str | None = None) -> None:
        node = f"{path}::{name}"
        outcome = {
            "ok": TestOutcome.PASSED,
            "F": TestOutcome.FAILED,
            "E": TestOutcome.ERROR,
            "T": TestOutcome.ERROR,
            "s": TestOutcome.SKIPPED,
            "f": TestOutcome.SKIPPED,
            "X": TestOutcome.FAILED,
        }[status]
        outcomes[node] = outcome
        if reason:
            blocks.setdefault(node, f"skipped: {reason}")

    for raw in text.splitlines():
        line = re.sub(r"\s*\[(?:OK|FAIL)\]\s*$", "", raw.strip())
        if not line:
            continue
        file_match = _SYMPY_FILE.match(line)
        if file_match:
            pending = None
            active_file = file_match.group("path")
            file_counts[active_file] = int(file_match.group("count"))
            continue
        block = _SYMPY_BLOCK.match(line)
        if block:
            node = f"{block.group('path')}::{block.group('name')}"
            blocks.setdefault(node, line)
            outcomes.setdefault(node, TestOutcome.FAILED)
            continue
        if pending is not None and line in {"ok", "F", "E", "T", "s", "f", "X"}:
            record(pending[0], pending[1], line)
            pending = None
            continue
        if not line.startswith("test_") or active_file is None:
            continue
        match = _SYMPY_TEST.match(line)
        if match is None:
            reason_match = _SYMPY_TEST_REASON.match(line)
            if reason_match is not None:
                record(
                    active_file,
                    reason_match.group("name"),
                    "s",
                    reason_match.group("reason"),
                )
                continue
            # The test name and its status char are written around the test
            # body; warnings can print in between. Hold the name and wait for
            # the standalone status line instead of dropping the test.
            pending = (active_file, line.split()[0])
            continue
        name = match.group("name")
        status = match.group("status")
        record(active_file, name, status)
    summary_match = _SYMPY_SUMMARY.search(text)
    if summary_match:
        for value, key in re.findall(r"(\d+)\s+([a-z][a-z ]*)", summary_match.group("body")):
            summary[key.strip()] = int(value)
    tests = [_case(node, outcome, blocks.get(node)) for node, outcome in sorted(outcomes.items())]
    kept, missing, extra = requested_filter(tests, requested)
    diagnostics: dict[str, Any] = {
        "runner": SYMPY,
        "parse_counts": {
            "parsed": len(tests),
            "wanted": len(requested) or None,
            "missing": len(missing),
            "extra_observed": len(extra),
            "files": file_counts,
            "summary": summary,
        },
        "requested_missing": list(missing),
        "requested_extra_observed": extra[:20],
    }
    if not tests:
        return NativeParse([], ExecutionStatus.COLLECTION_ERROR, "NO_TESTS_COLLECTED", diagnostics)
    if missing:
        return NativeParse(
            kept, ExecutionStatus.COLLECTION_ERROR, "REQUESTED_TESTS_NOT_COLLECTED", diagnostics
        )
    if timed_out:
        return NativeParse(kept, ExecutionStatus.TIMEOUT, "EXECUTION_TIMEOUT", diagnostics)
    failing = [test for test in kept if test.outcome in {TestOutcome.FAILED, TestOutcome.ERROR}]
    declared_failed = summary.get("failed", 0) + summary.get("exceptions", 0)
    diagnostics["summary_declares_failures"] = declared_failed
    if exit_code == 0 and (failing or declared_failed):
        return NativeParse(
            kept, ExecutionStatus.FAIL, "EXIT_CODE_OUTCOME_CONTRADICTION", diagnostics
        )
    if exit_code == 0:
        return NativeParse(kept, ExecutionStatus.PASS, None, diagnostics)
    if exit_code == 1:
        diagnostics["failing_nodes"] = [test.node_id for test in failing][:10]
        return NativeParse(kept, ExecutionStatus.FAIL, None, diagnostics)
    diagnostics["exit_code"] = exit_code
    return NativeParse(
        kept, ExecutionStatus.SANDBOX_ERROR, "UNEXPECTED_CONTAINER_EXIT", diagnostics
    )


class RunnerAdapter:
    """Command rewriting plus optional native parsing for one instance."""

    def __init__(self, config: RunnerConfig) -> None:
        self.config = config
        self.kind = config.kind
        self.native = config.kind in {DJANGO, SYMPY}

    def build_command(self, base: str, tests: tuple[str, ...], *, workspace: Path) -> str:
        if self.kind == PYTEST:
            return base
        if self.kind == PYTEST_REQUESTS_COMPAT:
            marker = "-m pytest"
            if marker in base:
                return base.replace(marker, f"{marker} -p codepilot_requests_compat", 1)
            tokens = base.split()
            if tokens and tokens[0].endswith("pytest"):
                return " ".join([tokens[0], "-p", "codepilot_requests_compat", *tokens[1:]])
            return base
        native_base = self.config.test_command
        if self.kind == DJANGO:
            labels = []
            for node in tests:
                selector = django_selector(node, workspace)
                if selector not in labels:
                    labels.append(selector)
            return " ".join([native_base, *labels]) if labels else native_base
        if self.kind == SYMPY:
            paths: list[str] = []
            for node in tests:
                path = sympy_selector(node)
                if path not in paths:
                    paths.append(path)
            return " ".join([native_base, *paths]) if paths else native_base
        raise ValueError(f"unsupported runner kind: {self.kind}")

    def parse(
        self,
        *,
        stdout: str,
        stderr: str,
        exit_code: int | None,
        workspace: Path,
        requested: tuple[str, ...],
        timed_out: bool = False,
    ) -> NativeParse | None:
        if self.kind == DJANGO:
            return parse_django(
                stdout,
                stderr,
                exit_code=exit_code,
                workspace=workspace,
                requested=requested,
                timed_out=timed_out,
            )
        if self.kind == SYMPY:
            return parse_sympy(
                stdout,
                stderr,
                exit_code=exit_code,
                workspace=workspace,
                requested=requested,
                timed_out=timed_out,
            )
        return None


def adapter_for(instance_id: str, overrides: RunnerOverrides | None) -> RunnerAdapter | None:
    if overrides is None:
        return None
    config = overrides.for_instance(instance_id)
    return RunnerAdapter(config) if config is not None else None
