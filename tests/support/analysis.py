from __future__ import annotations

from pathlib import Path

from codepilot.repo.context import AnalysisContext
from codepilot.repo.parser import TreeSitterParser


def make_context(sources: dict[str, str]) -> AnalysisContext:
    parser = TreeSitterParser()
    return AnalysisContext(
        "fixture",
        {path: parser.parse(source.encode(), path=path) for path, source in sources.items()},
    )


def fixture_context(fixtures_dir: Path, repo: str) -> AnalysisContext:
    root = fixtures_dir / "repos" / repo
    parser = TreeSitterParser()
    return AnalysisContext(
        "fixture",
        {
            file.relative_to(root).as_posix(): parser.parse(
                file.read_bytes(), path=file.relative_to(root).as_posix()
            )
            for file in root.rglob("*.py")
        },
    )
