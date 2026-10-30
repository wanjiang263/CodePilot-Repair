"""调用关系（R-09）：优先词法/导入解析，再作保守的同名方法近似。"""

from __future__ import annotations

from codepilot.repo.context import AnalysisContext
from codepilot.repo.imports import ImportResolver
from codepilot.repo.models import Confidence, GraphEdge, Symbol
from codepilot.repo.resolution import NameResolver, Resolution

MAX_METHOD_CANDIDATES = 5


class CallResolver:
    def __init__(
        self,
        context: AnalysisContext,
        imports: ImportResolver | None = None,
        only_paths: frozenset[str] | None = None,
    ) -> None:
        self.context = context
        self.names = NameResolver(context, imports or ImportResolver(context))
        # Incremental fast path: only extract call edges for these files
        # (repo/index.py proves unchanged sources keep byte-identical rows).
        self._files = (
            context.files
            if only_paths is None
            else tuple(file for file in context.files if file.path in only_paths)
        )
        # The conservative attribute fallback used to scan every symbol for
        # every unresolved call. Django has enough of both to make indexing
        # quadratic; build the same lookup once.
        methods: dict[str, list[Symbol]] = {}
        for symbol in context.symbols:
            if symbol.kind in {"method", "test_method"}:
                methods.setdefault(symbol.name, []).append(symbol)
        self._methods_by_name = {name: tuple(items) for name, items in methods.items()}

    def extract(self) -> tuple[GraphEdge, ...]:
        result = []
        for file in self._files:
            for node, owner in file.scoped_nodes():
                if node.kind != "call":
                    continue
                function = node.field("function")
                if function is None:
                    continue
                name = function.text
                resolution = self.names.resolve(owner, name)
                if name.startswith("self.") and name.count(".") == 1:
                    clazz = self.names.self_class(owner)
                    if clazz is not None:
                        resolution = self.names.methods(clazz, name.split(".")[1])
                elif function.kind == "attribute" and (
                    not resolution.known or self.names.local_value(owner, name.split(".")[0])
                ):
                    attribute = function.field("attribute")
                    if attribute is not None:
                        candidates = self._methods_by_name.get(attribute.text, ())
                        resolution = Resolution(
                            candidates if len(candidates) <= MAX_METHOD_CANDIDATES else ()
                        )
                targets: list[tuple[Symbol, Confidence]] = []
                for target in resolution.targets:
                    if target.kind == "class":
                        constructors = self.names.methods(target, "__init__")
                        targets.extend(
                            (
                                s,
                                "medium"
                                if resolution.confidence == "high"
                                and constructors.confidence == "medium"
                                else "low",
                            )
                            for s in constructors.targets
                        )
                    elif target.kind in {"function", "method", "test_function", "test_method"}:
                        targets.append((target, resolution.confidence))
                if targets:
                    for target, confidence in targets:
                        result.append(
                            GraphEdge(
                                owner.id, target.id, name, "CALL", confidence, node.start_line
                            )
                        )
                else:
                    result.append(GraphEdge(owner.id, None, name, "CALL", "low", node.start_line))
        return tuple(result)


def extract_calls(
    context: AnalysisContext,
    imports: ImportResolver | None = None,
    only_paths: frozenset[str] | None = None,
) -> tuple[GraphEdge, ...]:
    return CallResolver(context, imports, only_paths=only_paths).extract()
