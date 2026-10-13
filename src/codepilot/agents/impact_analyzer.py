"""Deterministic depth-one impact analysis and risk estimation (P-01/P-02)."""

from __future__ import annotations

from pathlib import PurePosixPath

from codepilot.repo.graph import GraphNeighbor, RepoGraph
from codepilot.repo.index import RepositoryIndex
from codepilot.repo.models import Symbol
from codepilot.schemas.enums import RiskLevel
from codepilot.schemas.impact import ImpactAnalysis, ImpactedSymbol, ImpactedTest, Relation
from codepilot.schemas.localization import LocalizationResult

__all__ = ["ImpactAnalyzer", "classify_risk"]


def classify_risk(
    symbols: list[Symbol],
    *,
    caller_counts: dict[str, int],
    subclassed_methods: set[str],
    signature_changed: set[str] | None = None,
) -> tuple[RiskLevel, list[str], bool]:
    """Classify from actual index evidence; unobserved patch facts remain estimates."""
    changed = signature_changed or set()
    reasons: list[str] = []
    verified = bool(signature_changed is not None)
    if any(symbol.qualified_name in changed for symbol in symbols):
        reasons.append("patch changes an indexed function signature")
    if any(PurePosixPath(symbol.path).name == "__init__.py" for symbol in symbols):
        reasons.append("target is exported through __init__.py")
    if any(symbol.qualified_name in subclassed_methods for symbol in symbols):
        reasons.append("public base method has an indexed subclass relationship")
    if reasons:
        return RiskLevel.HIGH, reasons, verified

    public_methods = [
        symbol for symbol in symbols if symbol.kind == "method" and not symbol.name.startswith("_")
    ]
    busy_functions = [
        symbol
        for symbol in symbols
        if symbol.kind == "function"
        and not symbol.name.startswith("_")
        and caller_counts.get(symbol.qualified_name, 0) > 5
    ]
    if public_methods or busy_functions:
        if public_methods:
            reasons.append("target includes a public class method")
        if busy_functions:
            reasons.append("public function has more than five indexed callers")
        return RiskLevel.MEDIUM, reasons, verified

    if symbols and all(symbol.name.startswith("_") for symbol in symbols):
        reasons.append("targets are private symbols; signature change is not yet evidenced")
    else:
        reasons.append("internal logic change is assumed until patch validation checks signatures")
    return RiskLevel.LOW, reasons, verified


class ImpactAnalyzer:
    """Read-only view over RepositoryIndex and RepoGraph."""

    def __init__(self, index: RepositoryIndex, graph: RepoGraph | None = None) -> None:
        self.index = index
        self.graph = graph or RepoGraph(index)

    def analyze(
        self,
        localization: LocalizationResult,
        *,
        signature_changed: set[str] | None = None,
    ) -> ImpactAnalysis:
        symbols_by_id = {symbol.id: symbol for symbol in self.index.symbol_objects()}
        selected: list[Symbol] = []
        missing: list[str] = []
        callers: list[ImpactedSymbol] = []
        callees: list[ImpactedSymbol] = []
        subclasses: list[ImpactedSymbol] = []
        tests: dict[str, ImpactedTest] = {}
        subclassed_methods: set[str] = set()

        for ranked in localization.symbols:
            symbol = symbols_by_id.get(ranked.symbol_id)
            if symbol is None or symbol.qualified_name != ranked.qualified_name:
                missing.append(ranked.symbol_id)
                continue
            selected.append(symbol)
            callers.extend(
                self._convert(self.graph.callers(symbol.qualified_name), Relation.CALLER)
            )
            callees.extend(
                self._convert(self.graph.callees(symbol.qualified_name), Relation.CALLEE)
            )
            for neighbor in self.graph.related_tests(symbol.qualified_name):
                tests.setdefault(
                    neighbor.symbol.path,
                    ImpactedTest(
                        path=neighbor.symbol.path,
                        symbol_id=neighbor.symbol.id,
                        qualified_name=neighbor.symbol.qualified_name,
                        confidence=neighbor.confidence,
                        source="TEST_edge",
                        evidence=f"TEST edge at source line {neighbor.line}",
                    ),
                )
            inherited = self._subclasses_for_method(symbol, symbols_by_id)
            if inherited:
                subclassed_methods.add(symbol.qualified_name)
                subclasses.extend(inherited)

        selected_dirs = {PurePosixPath(symbol.path).parent for symbol in selected}
        for file in self.index.files():
            path = PurePosixPath(file.path)
            if file.is_test and path.parent in selected_dirs:
                tests.setdefault(
                    file.path,
                    ImpactedTest(
                        path=file.path,
                        source="same_directory",
                        evidence="indexed test file shares the target directory",
                    ),
                )

        callers = _unique_impacts(callers)
        callees = _unique_impacts(callees)
        subclasses = _unique_impacts(subclasses)
        caller_counts = {
            symbol.qualified_name: len(self.graph.callers(symbol.qualified_name))
            for symbol in selected
        }
        risk, reasons, verified = classify_risk(
            selected,
            caller_counts=caller_counts,
            subclassed_methods=subclassed_methods,
            signature_changed=signature_changed,
        )
        test_evidence = sorted(tests.values(), key=lambda item: (item.path, item.source))
        return ImpactAnalysis(
            changed_symbols=[symbol.id for symbol in selected],
            callers=callers,
            callees=callees,
            subclasses=subclasses,
            related_tests=[item.path for item in test_evidence],
            related_test_evidence=test_evidence,
            risk=risk,
            risk_reasons=reasons,
            risk_estimated=not verified,
            missing_symbols=sorted(set(missing)),
        )

    @staticmethod
    def _convert(neighbors: list[GraphNeighbor], relation: Relation) -> list[ImpactedSymbol]:
        return [
            ImpactedSymbol(
                symbol_id=item.symbol.id,
                qualified_name=item.symbol.qualified_name,
                file=item.symbol.path,
                relation=relation,
                edge_kind=item.kind,
                confidence=item.confidence,
                evidence=f"{item.kind} edge at source line {item.line}",
            )
            for item in neighbors
        ]

    def _subclasses_for_method(
        self, method: Symbol, symbols_by_id: dict[str, Symbol]
    ) -> list[ImpactedSymbol]:
        if method.kind != "method" or method.name.startswith("_") or method.parent_id is None:
            return []
        parent = symbols_by_id.get(method.parent_id)
        if parent is None or parent.kind != "class":
            return []
        children = self.graph.adjacent(parent.qualified_name, ("INHERIT",), direction="in")
        return [
            ImpactedSymbol(
                symbol_id=child.symbol.id,
                qualified_name=child.symbol.qualified_name,
                file=child.symbol.path,
                relation=Relation.SUBCLASS,
                edge_kind=child.kind,
                confidence=child.confidence,
                evidence=f"INHERIT edge at source line {child.line}",
            )
            for child in children
        ]


def _unique_impacts(items: list[ImpactedSymbol]) -> list[ImpactedSymbol]:
    unique = {
        (item.symbol_id, item.relation, item.edge_kind): item
        for item in sorted(
            items,
            key=lambda item: (
                item.qualified_name,
                item.file,
                item.symbol_id,
                item.relation,
                item.edge_kind or "",
            ),
        )
    }
    return list(unique.values())
