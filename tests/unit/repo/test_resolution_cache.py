from __future__ import annotations

from codepilot.repo.imports import ImportResolver
from codepilot.repo.resolution import NameResolver
from support.analysis import make_context


def test_diamond_mro_and_name_cache_reuse_without_cross_context_leak() -> None:
    source = (
        "class A:\n    def f(self): pass\nclass B(A): pass\nclass C(A): pass\nclass D(B, C): pass\n"
    )
    context = make_context({"mod.py": source})
    resolver = NameResolver(context, ImportResolver(context))
    clazz = next(s for s in context.symbols if s.qualified_name == "mod.D")
    for _ in range(100):
        result = resolver.methods(clazz, "f")
        assert [s.qualified_name for s in result.targets] == ["mod.A.f"]
        assert result.confidence == "medium"
    assert [s.name for s in resolver._mro_cache[clazz.id]] == ["D", "B", "C", "A"]
    size = len(resolver._resolution_cache)
    resolver.methods(clazz, "f")
    assert len(resolver._resolution_cache) == size
    changed = make_context(
        {"mod.py": source.replace("class C(A): pass", "class C(A):\n    def f(self): pass")}
    )
    fresh = NameResolver(changed, ImportResolver(changed))
    d = next(s for s in changed.symbols if s.qualified_name == "mod.D")
    assert [s.qualified_name for s in fresh.methods(d, "f").targets] == ["mod.C.f"]


def test_cyclic_hierarchy_still_returns_conservative_resolution() -> None:
    context = make_context({"mod.py": "class A(B): pass\nclass B(A):\n    def f(self): pass\n"})
    resolver = NameResolver(context, ImportResolver(context))
    a = next(s for s in context.symbols if s.qualified_name == "mod.A")
    for _ in range(2):
        result = resolver.methods(a, "f")
        assert result.confidence == "low"
