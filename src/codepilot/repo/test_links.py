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
    only_paths: frozenset[str] | None = None,
) -> tuple[GraphEdge, ...]:
    imports = imports or ImportResolver(context, only_paths=only_paths)
    calls = extract_calls(context, imports, only_paths=only_paths) if calls is None else calls
    tests = {s.id: s for s in context.symbols if s.kind in {"test_function", "test_method"}}
    production = {s.id: s for s in context.symbols if not is_test_path(s.path)}
    if only_paths is not None:
        # Incremental fast path: keep TEST rows of untouched test files as-is.
        tests = {sid: s for sid, s in tests.items() if s.path in only_paths}
        calls = tuple(call for call in calls if call.source_id in tests)
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
    # Index production symbols once. The previous nested "for every test over
    # every import/target" shape was quadratic on Django and dominated index
    # time even after import lookup was indexed.
    module_cache: dict[str, str] = {}
    production_by_module: dict[str, list[Symbol]] = {}
    production_by_stem: dict[str, list[Symbol]] = {}
    for target in production.values():
        module = context.module_for(target)
        module_cache[target.id] = module
        production_by_module.setdefault(module, []).append(target)
        production_by_stem.setdefault(PurePosixPath(target.path).stem, []).append(target)
    for test in tests.values():
        scope_ids = {
            scope.id
            for scope in context.parents(test)
            if scope.kind != "class" or scope.id == test.id
        }
        modules: set[str] = set()
        for scope_id in scope_ids:
            for binding in imports.bindings_for_source(scope_id):
                if binding.target_module:
                    modules.add(binding.target_module)
                for target_id in binding.target_ids:
                    bound_target = context.by_id.get(target_id)
                    if bound_target is not None:
                        modules.add(module_cache.get(target_id) or context.module_for(bound_target))
        stem = PurePosixPath(test.path).stem
        counterpart = (
            stem.removeprefix("test_")
            if stem.startswith("test_")
            else stem.removesuffix("_test")
            if stem.endswith("_test")
            else None
        )
        lowered = test.name.casefold()
        for module in modules:
            for target in production_by_module.get(module, ()):
                if target.name.casefold() in lowered:
                    add(test, target, "medium", test.start_line)
        if counterpart:
            for target in production_by_stem.get(counterpart, ()):
                add(test, target, "low", test.start_line)
    return tuple(links[key] for key in sorted(links))
