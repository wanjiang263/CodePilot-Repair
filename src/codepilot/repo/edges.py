"""仓库内继承与静态符号引用（R-11）；复用调用解析的名称和类层级。"""

from __future__ import annotations

from codepilot.repo.context import AnalysisContext
from codepilot.repo.imports import ImportResolver
from codepilot.repo.models import GraphEdge
from codepilot.repo.parser import SyntaxNode, descendants
from codepilot.repo.resolution import NameResolver

NodeKey = tuple[str, int, int, int, int]


def _key(node: SyntaxNode) -> NodeKey:
    return node.kind, node.start_line, node.start_col, node.end_line, node.end_col


def _mask(node: SyntaxNode | None, excluded: set[NodeKey]) -> None:
    if node is not None:
        excluded.update(_key(child) for child in descendants(node))


def extract_edges(
    context: AnalysisContext, imports: ImportResolver | None = None
) -> tuple[GraphEdge, ...]:
    """返回 INHERIT 和 REFERENCE；声明、import 及调用目标不重复算引用。

    仅可静态绑定的仓库内名字生成 REFERENCE；未知对象属性不猜测目标。
    基类本身既是继承目标也是类定义的引用，外部基类不生成这两类边。
    """
    names = NameResolver(context, imports or ImportResolver(context))
    result: list[GraphEdge] = []
    for file in context.files:
        excluded: set[NodeKey] = set()
        nodes = tuple(file.scoped_nodes())
        for node, owner in nodes:
            if node.kind in {"import_statement", "import_from_statement"}:
                _mask(node, excluded)
            elif node.kind == "call":
                _mask(node.field("function"), excluded)
            elif node.kind in {"class_definition", "function_definition"}:
                _mask(node.field("name"), excluded)
                if node.kind == "class_definition":
                    for base in names.base_expressions(owner):
                        _mask(base, excluded)
                        expression = base.field("value") if base.kind == "subscript" else base
                        if expression is None:
                            continue
                        resolution = names.resolve(
                            context.by_id[owner.parent_id or ""], expression.text
                        )
                        for target in resolution.targets:
                            if target.kind == "class":
                                for kind in ("INHERIT", "REFERENCE"):
                                    result.append(
                                        GraphEdge(
                                            owner.id,
                                            target.id,
                                            base.text,
                                            kind,
                                            resolution.confidence,
                                            base.start_line,
                                        )
                                    )
            elif node.kind in {"parameters", "lambda_parameters"}:
                for parameter in node.children:
                    if parameter.kind in {
                        "identifier",
                        "list_splat_pattern",
                        "dictionary_splat_pattern",
                    }:
                        _mask(parameter, excluded)
                    else:
                        _mask(parameter.field("name"), excluded)
                        if parameter.kind == "typed_parameter" and parameter.children:
                            _mask(parameter.children[0], excluded)
            elif node.kind in {"assignment", "augmented_assignment", "for_statement"}:
                _mask(node.field("left"), excluded)
            elif node.kind in {"keyword_argument", "named_expression"}:
                _mask(node.field("name"), excluded)
        for node, owner in nodes:
            if node.kind not in {"identifier", "attribute"} or _key(node) in excluded:
                continue
            resolution = names.resolve(owner, node.text)
            if node.kind == "attribute":
                _mask(node, excluded)
                if node.text.startswith("self.") and node.text.count(".") == 1:
                    clazz = names.self_class(owner)
                    if clazz is not None:
                        resolution = names.methods(clazz, node.text.split(".")[1])
            for target in resolution.targets:
                result.append(
                    GraphEdge(
                        owner.id,
                        target.id,
                        node.text,
                        "REFERENCE",
                        resolution.confidence,
                        node.start_line,
                    )
                )
    return tuple(dict.fromkeys(result))
