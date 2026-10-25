"""名称与仓库内类层级的共享解析原语，不生成图或执行源码。"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from codepilot.repo.context import AnalysisContext
from codepilot.repo.imports import ImportResolver
from codepilot.repo.models import Confidence, Symbol
from codepilot.repo.parser import SyntaxNode


@dataclass(frozen=True, slots=True)
class Resolution:
    targets: tuple[Symbol, ...] = ()
    confidence: Confidence = "low"
    known: bool = False


class NameResolver:
    def __init__(self, context: AnalysisContext, imports: ImportResolver) -> None:
        self.context = context
        self.imports = imports
        self.assignments: dict[str, set[str]] = {}
        self.parameters: dict[str, set[str]] = {}
        self.class_nodes: dict[str, SyntaxNode] = {}
        for file in context.files:
            for node, owner in file.scoped_nodes():
                if node.kind == "class_definition":
                    self.class_nodes[owner.id] = node
                if node.kind == "function_definition":
                    try:
                        parsed = ast.parse((owner.signature or "") + ":\n    pass\n").body[0]
                    except (SyntaxError, IndexError):
                        continue
                    if isinstance(parsed, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        args = parsed.args
                        self.parameters[owner.id] = {
                            a.arg
                            for a in [
                                *args.posonlyargs,
                                *args.args,
                                *args.kwonlyargs,
                                *([args.vararg] if args.vararg else []),
                                *([args.kwarg] if args.kwarg else []),
                            ]
                        }
                if node.kind in {
                    "assignment",
                    "augmented_assignment",
                    "for_statement",
                    "named_expression",
                    "as_pattern",
                    "for_in_clause",
                }:
                    left = node.field("left") or node.field("name") or node.field("alias")
                    self.assignments.setdefault(owner.id, set()).update(_pattern_names(left))
                if node.kind == "lambda_parameters":
                    # lambda 不创建索引符号；保守阻止其参数误绑到外层同名定义。
                    bound = set()
                    for parameter in node.children:
                        if parameter.kind in {
                            "identifier",
                            "list_splat_pattern",
                            "dictionary_splat_pattern",
                        }:
                            bound.update(_pattern_names(parameter))
                        else:
                            bound.update(_pattern_names(parameter.field("name")))
                    self.assignments.setdefault(owner.id, set()).update(bound)

    def resolve(self, owner: Symbol, expression: str) -> Resolution:
        parts = expression.split(".")
        if not all(part.isidentifier() for part in parts):
            return Resolution()
        current = self._bare(owner, parts[0])
        # import a.b binds a, while the recorded imported module is a.b.
        bindings = self.imports.for_scope(owner, parts[0])
        if (
            len(parts) > 1
            and not self.local_value(owner, parts[0])
            and not any(b.alias is not None or b.name is not None for b in bindings)
        ):
            for binding in bindings:
                if (
                    binding.name is None
                    and binding.alias is None
                    and expression.startswith(binding.module + ".")
                    and binding.target_module
                ):
                    current = Resolution(
                        self.context.module_symbols(binding.target_module), "high", True
                    )
                    parts = [binding.module, *expression[len(binding.module) + 1 :].split(".")]
                    break
        for part in parts[1:]:
            targets: list[Symbol] = []
            for parent in current.targets:
                targets.extend(self.context.named(parent, part))
                if parent.kind == "module":
                    targets.extend(self.context.module_symbols(parent.qualified_name + "." + part))
                    for binding in self.imports.for_scope(parent, part):
                        targets.extend(self.context.by_id[id_] for id_ in binding.target_ids)
            unique = tuple({s.id: s for s in targets}.values())
            current = Resolution(
                unique,
                "high" if len(unique) == 1 and current.confidence == "high" else "low",
                current.known,
            )
        return current

    def _blocked(self, owner: Symbol, name: str) -> bool:
        return name in self.parameters.get(owner.id, set()) or name in self.assignments.get(
            owner.id, set()
        )

    def local_value(self, owner: Symbol, name: str) -> bool:
        for scope in self.context.parents(owner):
            if scope.kind == "class" and scope.id != owner.id:
                continue
            if self._blocked(scope, name):
                return True
            if self.context.named(scope, name) or self.imports.bindings_in_scope(scope.id, name):
                return False
        return False

    def _bare(self, owner: Symbol, name: str) -> Resolution:
        for scope in self.context.parents(owner):
            if scope.kind == "class" and scope.id != owner.id:
                continue
            if self._blocked(scope, name):
                return Resolution(known=True)
            definitions = self.context.named(scope, name)
            bindings = self.imports.bindings_in_scope(scope.id, name)
            if definitions or bindings:
                targets = tuple(
                    {
                        s.id: s
                        for s in [
                            *definitions,
                            *(
                                target
                                for b in bindings
                                for target in (
                                    self.context.module_symbols(b.bound_name)
                                    if b.name is None and b.alias is None and "." in b.module
                                    else tuple(self.context.by_id[id_] for id_ in b.target_ids)
                                )
                            ),
                        ]
                    }.values()
                )
                # 未解析的同名 import 也是一个遮蔽候选，不可当作确定解析。
                certain = len(targets) == 1 and not any(not b.target_ids for b in bindings)
                return Resolution(targets, "high" if certain else "low", True)
            if self.imports.has_wildcard(scope.id):
                return Resolution(known=True)
        return Resolution()

    def self_class(self, owner: Symbol) -> Symbol | None:
        for scope in self.context.parents(owner):
            if "self" in self.assignments.get(scope.id, set()):
                return None
            if "self" in self.parameters.get(scope.id, set()):
                parent = self.context.by_id.get(scope.parent_id or "")
                return parent if parent is not None and parent.kind == "class" else None
        return None

    def base_expressions(self, clazz: Symbol) -> tuple[SyntaxNode, ...]:
        # 增量上下文中，未变化文件的类没有解析树（R-16）：返回空，让 MRO 退化为
        # 只用该类自身的方法表，而不是在这里抛 KeyError 中断整个提取。
        node = self.class_nodes.get(clazz.id)
        if node is None:
            return ()
        superclasses = node.field("superclasses")
        return (
            tuple(
                child
                for child in superclasses.children
                if child.kind not in {"(", ")", ",", "keyword_argument", "comment"}
            )
            if superclasses
            else ()
        )

    def bases(self, clazz: Symbol) -> tuple[Symbol, ...]:
        result: list[Symbol] = []
        for node in self.base_expressions(clazz):
            expression = node.field("value") if node.kind == "subscript" else node
            if expression is not None:
                resolution = self.resolve(
                    self.context.by_id[clazz.parent_id or ""], expression.text
                )
                result.extend(s for s in resolution.targets if s.kind == "class")
        return tuple(result)

    def methods(self, clazz: Symbol, name: str) -> Resolution:
        direct = self.context.named(clazz, name)
        if direct:
            return Resolution(direct, "medium" if len(direct) == 1 else "low", True)
        order = self._mro(clazz, frozenset())
        if order is not None:
            for parent in order[1:]:
                methods = self.context.named(parent, name)
                if methods:
                    return Resolution(methods, "medium" if len(methods) == 1 else "low", True)
        # 不一致的层级、条件基类或循环不作确定绑定。
        found: dict[str, Symbol] = {}
        visited = {clazz.id}
        stack = list(self.bases(clazz))
        while stack:
            parent = stack.pop()
            if parent.id in visited:
                continue
            visited.add(parent.id)
            found.update((s.id, s) for s in self.context.named(parent, name))
            stack.extend(self.bases(parent))
        return Resolution(tuple(found.values()), "low", True)

    def _mro(self, clazz: Symbol, trail: frozenset[str]) -> list[Symbol] | None:
        if clazz.id in trail:
            return None
        for node in self.base_expressions(clazz):
            expression = node.field("value") if node.kind == "subscript" else node
            if expression is None:
                return None
            base = self.resolve(self.context.by_id[clazz.parent_id or ""], expression.text)
            if (
                base.confidence != "high"
                or len(base.targets) != 1
                or base.targets[0].kind != "class"
            ):
                return None
        parents = self.bases(clazz)
        sequences: list[list[Symbol]] = []
        for parent in parents:
            order = self._mro(parent, trail | {clazz.id})
            if order is None:
                return None
            sequences.append(order[:])
        sequences.append(list(parents))
        result = [clazz]
        while any(sequences):
            sequences = [seq for seq in sequences if seq]
            head = next(
                (
                    seq[0]
                    for seq in sequences
                    if not any(seq[0] in other[1:] for other in sequences)
                ),
                None,
            )
            if head is None:
                return None
            result.append(head)
            for seq in sequences:
                if seq[0] == head:
                    seq.pop(0)
        return result


def _pattern_names(node: SyntaxNode | None) -> set[str]:
    if node is None:
        return set()
    if node.kind == "identifier":
        return {node.text}
    if node.kind in {"attribute", "subscript", "type"}:
        return set()
    return set().union(*(_pattern_names(child) for child in node.children))
