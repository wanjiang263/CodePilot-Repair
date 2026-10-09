"""静态 Test → Symbol 关联（R-12），按测试/目标 ID 保留最强证据。"""

from __future__ import annotations

from pathlib import PurePosixPath

from codepilot.repo.calls import extract_calls
from codepilot.repo.context import AnalysisContext
from codepilot.repo.imports import ImportResolver
from codepilot.repo.models import Confidence, GraphEdge, Symbol
from codepilot.repo.scanner import is_test_path

_STRENGTH = {"low": 0, "medium": 1, "high": 2}


def extract_test_links(
    context: AnalysisContext,
    imports: ImportResolver | None = None,
    calls: tuple[GraphEdge, ...] | None = None,
) -> tuple[GraphEdge, ...]:
    imports = imports or ImportResolver(context)
    calls = extract_calls(context, imports) if calls is None else calls
    tests = {s.id: s for s in context.symbols if s.kind in {"test_function", "test_method"}}
    production = {s.id: s for s in context.symbols if not is_test_path(s.path)}
    links: dict[tuple[str, str], GraphEdge] = {}

    def add(test: Symbol, target: Symbol, confidence: Confidence, line: int) -> None:
        key = test.id, target.id
        previous = links.get(key)
        if previous is None or _STRENGTH[confidence] > _STRENGTH[previous.confidence]:
            links[key] = GraphEdge(
                test.id, target.id, target.qualified_name, "TEST", confidence, line
            )

    for call in calls:
        if call.kind != "CALL" or call.source_id not in tests or call.target_id not in production:
            continue
        # 确定的直接调用（包括明确类构造/self 方法）为 high。
        # 未知对象的同名候选和重复定义仍为 low，不把猜测提升为直接调用证据。
        add(
            tests[call.source_id],
            production[call.target_id],
            "low" if call.confidence == "low" else "high",
            call.line,
        )
    for test in tests.values():
        scope_ids = {
            scope.id
            for scope in context.parents(test)
            if scope.kind != "class" or scope.id == test.id
        }
        modules: set[str] = set()
        for binding in imports.bindings:
            if binding.source_id not in scope_ids:
                continue
            if binding.target_module:
                modules.add(binding.target_module)
            modules.update(context.module_for(context.by_id[id_]) for id_ in binding.target_ids)
        stem = PurePosixPath(test.path).stem
        counterpart = (
            stem.removeprefix("test_")
            if stem.startswith("test_")
            else stem.removesuffix("_test")
            if stem.endswith("_test")
            else None
        )
        for target in production.values():
            if (
                context.module_for(target) in modules
                and target.name.casefold() in test.name.casefold()
            ):
                add(test, target, "medium", test.start_line)
            if counterpart and PurePosixPath(target.path).stem == counterpart:
                add(test, target, "low", test.start_line)
    return tuple(links[key] for key in sorted(links))
