"""Import 提取与仓库内目标解析（R-08），包含包再导出与循环保护。"""

from __future__ import annotations

from dataclasses import replace

from codepilot.repo.context import AnalysisContext, SourceFile
from codepilot.repo.models import Confidence, GraphEdge, ImportBinding, Symbol
from codepilot.repo.parser import SyntaxNode


class ImportResolver:
    def __init__(self, context: AnalysisContext) -> None:
        self.context = context
        self._raw = tuple(binding for file in context.files for binding in self._extract(file))
        self.bindings = tuple(
            replace(binding, target_ids=tuple(s.id for s in self.targets(binding)))
            for binding in self._raw
        )

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
            bindings = tuple(
                b for b in self.bindings if b.source_id == scope.id and b.bound_name == name
            )
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
            for exported in self._raw:
                if (
                    exported.source_id == parent.id
                    and exported.bound_name == binding.name
                    and not exported.wildcard
                ):
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
