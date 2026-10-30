"""Import 提取与仓库内目标解析（R-08），包含包再导出与循环保护。"""

from __future__ import annotations

from dataclasses import replace

from codepilot.repo.context import AnalysisContext, SourceFile
from codepilot.repo.models import Confidence, GraphEdge, ImportBinding, Symbol
from codepilot.repo.parser import SyntaxNode


class ImportResolver:
    def __init__(self, context: AnalysisContext, only_paths: frozenset[str] | None = None) -> None:
        """``only_paths`` restricts raw-binding *extraction* to those files.

        Used by the incremental fast path (repo/index.py): a source outside the
        set keeps its stored edges untouched. Resolution itself still sees the
        full context, and a changed file's scope only ever consults its own
        imports, so restricted extraction cannot change any kept row.
        """
        self.context = context
        files = (
            context.files
            if only_paths is None
            else tuple(file for file in context.files if file.path in only_paths)
        )
        self._raw = tuple(binding for file in files for binding in self._extract(file))
        self._raw_by_scope_name = _group_by_scope_name(self._raw)
        self.bindings = tuple(
            replace(binding, target_ids=tuple(s.id for s in self.targets(binding)))
            for binding in self._raw
        )
        # Call/name resolution asks "which imports are visible under this exact
        # scope and name?" once per call site. Scanning every binding for every
        # scope made Django indexing quadratic (observed >20 minutes with the
        # process still in for_scope). Index the same data by its lookup key.
        self._bindings_by_scope_name = _group_by_scope_name(self.bindings)
        self._bindings_by_source: dict[str, tuple[ImportBinding, ...]] = {}
        grouped_by_source: dict[str, list[ImportBinding]] = {}
        for binding in self.bindings:
            grouped_by_source.setdefault(binding.source_id, []).append(binding)
        self._bindings_by_source = {key: tuple(value) for key, value in grouped_by_source.items()}
        self._wildcard_scopes = frozenset(b.source_id for b in self.bindings if b.wildcard)

    def bindings_in_scope(self, scope_id: str, name: str) -> tuple[ImportBinding, ...]:
        return self._bindings_by_scope_name.get((scope_id, name), ())

    def bindings_for_source(self, source_id: str) -> tuple[ImportBinding, ...]:
        return self._bindings_by_source.get(source_id, ())

    def has_wildcard(self, scope_id: str) -> bool:
        return scope_id in self._wildcard_scopes

    def _extract(self, file: SourceFile) -> list[ImportBinding]:
        found = []
        for node, owner in file.scoped_nodes():
            if node.kind not in {"import_statement", "import_from_statement"}:
                continue
            module_node = node.field("module_name")
            from_module = module_node.text if module_node is not None else None
            entries = list(node.field_all("name"))
            entries.extend(child for child in node.children if child.kind == "wildcard_import")
            for entry in entries:
                name, alias = _entry(entry)
                raw_module = name if from_module is None else from_module
                absolute = _absolute_module(file, raw_module)
                internal = absolute is not None and self.context.internal_module(absolute)
                imported_name = None if from_module is None else name
                target_module = absolute if internal else None
                if (
                    internal
                    and imported_name is not None
                    and self.context.internal_module(f"{absolute}.{name}")
                ):
                    target_module = f"{absolute}.{name}"
                found.append(
                    ImportBinding(
                        source_id=owner.id,
                        module=raw_module,
                        name=imported_name,
                        alias=alias,
                        bound_name=alias or (name.split(".")[0] if from_module is None else name),
                        target_module=target_module,
                        target_ids=(),
                        external=not internal and not raw_module.startswith("."),
                        line=node.start_line,
                        wildcard=name == "*",
                    )
                )
        return found

    def for_scope(self, owner: Symbol, name: str) -> tuple[ImportBinding, ...]:
        for scope in self.context.parents(owner):
            # 方法不能闭包捕获类体里的 import；模块和外层函数可以。
            if scope.kind == "class" and scope.id != owner.id:
                continue
            bindings = self._bindings_by_scope_name.get((scope.id, name), ())
            if bindings:
                return bindings
        return ()

    def targets(
        self, binding: ImportBinding, trail: frozenset[tuple[str, str]] = frozenset()
    ) -> tuple[Symbol, ...]:
        module = binding.target_module
        if module is None:
            return ()
        marker = (module, binding.name or "")
        if marker in trail:
            return ()
        source = self.context.by_id[binding.source_id]
        import_base = _absolute_module(self.context.file_for(source), binding.module)
        if binding.name is None or binding.wildcard or module != import_base:
            return self.context.module_symbols(module)
        result: list[Symbol] = []
        for parent in self.context.module_symbols(module):
            direct = self.context.named(parent, binding.name)
            if direct:
                result.extend(direct)
                continue
            for exported in self._raw_by_scope_name.get((parent.id, binding.name), ()):
                if not exported.wildcard:
                    result.extend(self.targets(exported, trail | {marker}))
        return tuple({symbol.id: symbol for symbol in result}.values())

    def edges(self) -> tuple[GraphEdge, ...]:
        result = []
        for binding in self.bindings:
            targets: tuple[str | None, ...] = binding.target_ids or (None,)
            confidence: Confidence = (
                "high" if len(binding.target_ids) == 1 and not binding.wildcard else "low"
            )
            for target in targets:
                result.append(
                    GraphEdge(
                        binding.source_id,
                        target,
                        binding.module + ("." + binding.name if binding.name else ""),
                        "IMPORT",
                        confidence,
                        binding.line,
                    )
                )
        return tuple(result)


def _group_by_scope_name(
    bindings: tuple[ImportBinding, ...],
) -> dict[tuple[str, str], tuple[ImportBinding, ...]]:
    grouped: dict[tuple[str, str], list[ImportBinding]] = {}
    for binding in bindings:
        grouped.setdefault((binding.source_id, binding.bound_name), []).append(binding)
    return {key: tuple(value) for key, value in grouped.items()}


def extract_imports(context: AnalysisContext) -> tuple[ImportBinding, ...]:
    return ImportResolver(context).bindings


def _entry(node: SyntaxNode) -> tuple[str, str | None]:
    if node.kind == "aliased_import":
        name, alias = node.field("name"), node.field("alias")
        return (name.text if name is not None else node.text), (
            alias.text if alias is not None else None
        )
    return node.text.strip(), None


def _absolute_module(file: SourceFile, raw: str) -> str | None:
    if not raw.startswith("."):
        return raw
    level = len(raw) - len(raw.lstrip("."))
    package = (
        file.module.split(".")
        if file.path.endswith("/__init__.py")
        else file.module.split(".")[:-1]
    )
    if level > len(package):
        return None
    base = package[: len(package) - level + 1]
    suffix = raw[level:]
    return ".".join([*base, *([suffix] if suffix else [])])
