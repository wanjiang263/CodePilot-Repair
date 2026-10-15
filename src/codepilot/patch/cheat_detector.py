"""AST-delta cheat heuristics. Findings are scoped to newly introduced behavior (P-16/P-17)."""

from __future__ import annotations

import ast
from collections import Counter
from collections.abc import Iterable
from typing import Any

from codepilot.schemas.validation import CheatFinding, CheatPattern

__all__ = ["CheatDetectionError", "detect_ast_cheats", "detect_hardcoded_expectations"]


class CheatDetectionError(ValueError):
    pass


def detect_ast_cheats(before: str, after: str, path: str) -> list[CheatFinding]:
    try:
        old_tree = ast.parse(before, filename=path)
        new_tree = ast.parse(after, filename=path)
    except SyntaxError as exc:
        raise CheatDetectionError(f"cannot compare invalid AST for {path}: {exc}") from exc
    findings: list[CheatFinding] = []
    old_tests = _test_functions(old_tree)
    new_tests = _test_functions(new_tree)
    for name, node in sorted(old_tests.items()):
        if name not in new_tests:
            findings.append(
                _finding(
                    CheatPattern.DELETED_TEST,
                    "CP-CHEAT-001",
                    path,
                    node.lineno,
                    f"test function removed: {name}",
                )
            )

    for name in sorted(new_tests.keys() - old_tests.keys()):
        node = new_tests[name]
        assertions = _assert_nodes(node)
        has_call = any(isinstance(item, ast.Call) for item in ast.walk(node))
        if (not assertions and not has_call) or any(_always_true(item.test) for item in assertions):
            findings.append(
                _finding(
                    CheatPattern.WEAKENED_ASSERTION,
                    "CP-CHEAT-NEW",
                    path,
                    node.lineno,
                    "new test has empty or always-true assertions",
                )
            )

    old_skip = _skip_nodes(old_tree)
    for signature, skip_node in (_skip_nodes(new_tree) - old_skip).items():
        findings.append(
            _finding(
                CheatPattern.SKIPPED_TEST,
                "CP-CHEAT-002",
                path,
                getattr(skip_node, "lineno", 1),
                f"new skip/skipif/xfail behavior: {signature}",
            )
        )

    for name in sorted(old_tests.keys() & new_tests.keys()):
        old_asserts = _assert_nodes(old_tests[name])
        new_asserts = _assert_nodes(new_tests[name])
        if len(new_asserts) < len(old_asserts):
            findings.append(
                _finding(
                    CheatPattern.WEAKENED_ASSERTION,
                    "CP-CHEAT-003",
                    path,
                    new_tests[name].lineno,
                    f"assertion count decreased in {name}: {len(old_asserts)} -> {len(new_asserts)}",
                )
            )
        old_true = Counter(_dump(node) for node in old_asserts if _always_true(node.test))
        for assert_node in new_asserts:
            signature = _dump(assert_node)
            if _always_true(assert_node.test) and old_true[signature] == 0:
                findings.append(
                    _finding(
                        CheatPattern.WEAKENED_ASSERTION,
                        "CP-CHEAT-004",
                        path,
                        assert_node.lineno,
                        f"assertion changed to an always-true expression in {name}",
                    )
                )
            elif old_true[signature]:
                old_true[signature] -= 1

    findings.extend(
        _new_node_findings(
            old_tree,
            new_tree,
            path,
            predicate=_swallows_exception,
            pattern=CheatPattern.SWALLOWED_EXCEPTION,
            rule_id="CP-CHEAT-005",
            detail="new broad exception handler swallows the exception with pass",
        )
    )
    findings.extend(
        _new_node_findings(
            old_tree,
            new_tree,
            path,
            predicate=_environment_probe,
            pattern=CheatPattern.ENVIRONMENT_PROBE,
            rule_id="CP-CHEAT-006",
            detail="new pytest environment probe",
        )
    )
    return _deduplicate(findings)


def detect_hardcoded_expectations(
    before: str,
    after: str,
    path: str,
    related_test_sources: dict[str, str],
) -> list[CheatFinding]:
    try:
        old_tree = ast.parse(before, filename=path)
        new_tree = ast.parse(after, filename=path)
        tests = {
            test_path: ast.parse(source, filename=test_path)
            for test_path, source in related_test_sources.items()
        }
    except SyntaxError as exc:
        raise CheatDetectionError(f"cannot run hardcode heuristic: {exc}") from exc
    old_functions = _functions(old_tree)
    new_functions = _functions(new_tree)
    asserted: dict[str | int | float | complex, list[tuple[str, int]]] = {}
    for test_path, tree in tests.items():
        for assertion in (node for node in ast.walk(tree) if isinstance(node, ast.Assert)):
            for value in _constants(assertion.test):
                asserted.setdefault(value, []).append((test_path, assertion.lineno))

    findings: list[CheatFinding] = []
    for name, new_node in new_functions.items():
        old_node = old_functions.get(name)
        if old_node is not None and _dump(old_node) == _dump(new_node):
            continue
        old_values = Counter(_constants(old_node)) if old_node is not None else Counter()
        for value in _constants(new_node):
            if old_values[value]:
                old_values[value] -= 1
                continue
            if value not in asserted:
                continue
            evidence = ", ".join(f"{test_path}:{line}" for test_path, line in asserted[value][:3])
            findings.append(
                CheatFinding(
                    pattern=CheatPattern.HARDCODED_EXPECTATION,
                    rule_id="CP-CHEAT-W001",
                    file=path,
                    line=new_node.lineno,
                    detail=f"new constant {value!r} in {name} also appears in test assertions ({evidence})",
                    reject=False,
                )
            )
    return _deduplicate(findings)


def _test_functions(tree: ast.AST) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    }


def _functions(tree: ast.AST) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("test_")
    }


class _NodeMap(dict[str, ast.AST]):
    def __sub__(self, other: _NodeMap) -> _NodeMap:
        return _NodeMap({key: value for key, value in self.items() if key not in other})


def _skip_nodes(tree: ast.AST) -> _NodeMap:
    result = _NodeMap()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for decorator in node.decorator_list:
                name = _call_name(decorator.func if isinstance(decorator, ast.Call) else decorator)
                if name.endswith((".skip", ".skipif", ".xfail")):
                    result[_dump(decorator)] = decorator
        if isinstance(node, ast.Call) and _call_name(node.func) == "pytest.skip":
            result[_dump(node)] = node
    return result


def _assert_nodes(node: ast.AST) -> list[ast.Assert]:
    return [child for child in ast.walk(node) if isinstance(child, ast.Assert)]


def _always_true(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return bool(node.value)
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and len(node.comparators) == 1:
        return isinstance(node.ops[0], (ast.Eq, ast.Is, ast.LtE, ast.GtE)) and _dump(
            node.left
        ) == _dump(node.comparators[0])
    return False


def _swallows_exception(node: ast.AST) -> bool:
    if not isinstance(node, ast.ExceptHandler) or not node.body:
        return False
    catches_broad = node.type is None or (
        isinstance(node.type, ast.Name) and node.type.id in {"Exception", "BaseException"}
    )
    return catches_broad and all(isinstance(statement, ast.Pass) for statement in node.body)


def _environment_probe(node: ast.AST) -> bool:
    if isinstance(node, ast.Call) and _call_name(node.func) in {
        "os.getenv",
        "os.environ.get",
    }:
        return bool(
            node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "PYTEST_CURRENT_TEST"
        )
    if isinstance(node, ast.Subscript):
        return (
            _call_name(node.value) == "sys.modules"
            and isinstance(node.slice, ast.Constant)
            and node.slice.value == "pytest"
        )
    return False


def _new_node_findings(
    old_tree: ast.AST,
    new_tree: ast.AST,
    path: str,
    *,
    predicate: Any,
    pattern: CheatPattern,
    rule_id: str,
    detail: str,
) -> list[CheatFinding]:
    old = Counter(_dump(node) for node in ast.walk(old_tree) if predicate(node))
    findings: list[CheatFinding] = []
    for node in ast.walk(new_tree):
        if not predicate(node):
            continue
        signature = _dump(node)
        if old[signature]:
            old[signature] -= 1
            continue
        findings.append(_finding(pattern, rule_id, path, getattr(node, "lineno", 1), detail))
    return findings


def _constants(node: ast.AST | None) -> list[str | int | float | complex]:
    if node is None:
        return []
    result: list[str | int | float | complex] = []
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Constant)
            and isinstance(child.value, (str, int, float, complex))
            and not isinstance(child.value, bool)
            and child.value not in {"", 0, 1}
        ):
            result.append(child.value)
    return result


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _dump(node: ast.AST) -> str:
    return ast.dump(node, annotate_fields=True, include_attributes=False)


def _finding(
    pattern: CheatPattern, rule_id: str, path: str, line: int, detail: str
) -> CheatFinding:
    return CheatFinding(
        pattern=pattern,
        rule_id=rule_id,
        file=path,
        line=line,
        detail=detail,
        reject=True,
    )


def _deduplicate(findings: Iterable[CheatFinding]) -> list[CheatFinding]:
    unique = {
        (item.rule_id, item.file, item.line, item.detail): item
        for item in sorted(
            findings,
            key=lambda item: (item.file, item.line or 0, item.rule_id, item.detail),
        )
    }
    return list(unique.values())
