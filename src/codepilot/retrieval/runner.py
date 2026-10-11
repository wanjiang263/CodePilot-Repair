"""Gold-free offline repository indexing and retrieval used by both CLIs.

Git sources are checked out through WorkspaceManager at a resolved base SHA.
Directory fixtures are copied into a private, deterministically committed Git
snapshot; the original tree is never initialized, edited or executed.
"""

import math
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codepilot.fsutil import remove_tree
from codepilot.repo.git_history import FileChurn, GitHistory
from codepilot.repo.gitcmd import run_git
from codepilot.repo.graph import RepoGraph
from codepilot.repo.index import Indexer, RepositoryIndex
from codepilot.repo.models import Symbol
from codepilot.repo.workspace import WorkspaceManager, normalize_source, repository_id
from codepilot.retrieval.config import CHANNELS, RetrievalConfig, SeedChannel
from codepilot.retrieval.input import RetrievalInput
from codepilot.retrieval.offline import OFFLINE_DESCRIPTION, OfflineHashEmbedding
from codepilot.retrieval.service import retrieve_symbols
from codepilot.schemas.retrieval import FusionMethod, RetrievalResult
from codepilot.storage.db import (
    create_engine_from_url,
    create_session_factory,
    init_db,
    session_scope,
)
from codepilot.storage.repository import Stores


class RetrievalOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    channels: RetrievalConfig = Field(default_factory=RetrievalConfig)
    method: FusionMethod | None = None
    weights: dict[str, float] | None = None
    rrf_k: int = Field(default=60, ge=1)
    seed_channel: SeedChannel = "bm25"
    embedding: Literal["none", "offline-hash"] = "none"

    @model_validator(mode="after")
    def check_options(self) -> "RetrievalOptions":
        if self.channels.semantic and self.embedding == "none":
            raise ValueError("semantic requires an explicit embedding mode (offline-hash)")
        if self.method == FusionMethod.NONE:
            raise ValueError("method must be weighted or rrf, or omitted for legacy raw ranking")
        if self.rrf_k != 60 and self.method != FusionMethod.RRF:
            raise ValueError("rrf_k requires method=rrf")
        if self.weights is not None:
            if self.method == FusionMethod.RRF:
                raise ValueError("weights apply only to weighted fusion")
            if any(
                name not in (*CHANNELS, "lexical") or not math.isfinite(weight) or weight < 0
                for name, weight in self.weights.items()
            ):
                raise ValueError("weights require known channels and finite nonnegative values")
            if "lexical" in self.weights and {"bm25", "ripgrep"} & self.weights.keys():
                raise ValueError("lexical cannot be mixed with bm25/ripgrep weights")
        return self


class ExecutionInfo(BaseModel):
    source_kind: Literal["git", "directory_snapshot"]
    base_commit: str
    embedding_description: str
    structural_seed: str | None
    options: RetrievalOptions


@dataclass(frozen=True)
class IndexedRepository:
    root: Path
    symbols: tuple[Symbol, ...]
    graph: RepoGraph
    churn: dict[str, FileChurn]
    base_commit: str
    source_kind: Literal["git", "directory_snapshot"]

    def retrieve(
        self, task: RetrievalInput, options: RetrievalOptions, *, limit: int = 20
    ) -> tuple[RetrievalResult, ExecutionInfo]:
        provider = OfflineHashEmbedding() if options.embedding == "offline-hash" else None
        seeds = None
        standalone_seed = options.channels.structural and not getattr(
            options.channels, options.seed_channel
        )
        if standalone_seed:
            # Dependency-only seed calculation, never an enabled output channel.
            seeds = retrieve_symbols(
                self.root,
                self.symbols,
                task.issue,
                channel=options.seed_channel,
                embedding_provider=provider,
            ).per_channel[options.seed_channel]
        result = retrieve_symbols(
            self.root,
            self.symbols,
            task.issue,
            config=options.channels,
            fusion_method=options.method,
            channel_weights=options.weights,
            rrf_k=options.rrf_k,
            seed_channel=options.seed_channel,
            seeds=seeds,
            embedding_provider=provider,
            graph=self.graph,
            churn=self.churn,
            limit=limit,
        )
        info = ExecutionInfo(
            source_kind=self.source_kind,
            base_commit=self.base_commit,
            embedding_description=OFFLINE_DESCRIPTION if provider else "not enabled",
            structural_seed=(
                f"{options.seed_channel} Top-20 (seed source only)"
                if standalone_seed
                else options.seed_channel
                if options.channels.structural
                else None
            ),
            options=options,
        )
        return result, info


def _snapshot(source: Path, target: Path) -> None:
    target.mkdir()
    excluded = {".git", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if any(part in excluded for part in relative.parts):
            continue
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise ValueError(f"Snapshot does not accept symlinks/junctions: {relative}")
        if path.is_file():
            out = target / relative
            out.parent.mkdir(parents=True, exist_ok=True)
            # Canonicalize source EOLs only in the private fixture copy.
            content = path.read_bytes()
            out.write_bytes(
                content.replace(b"\r\n", b"\n")
                if path.suffix in {".py", ".md", ".toml", ".txt"}
                else content
            )
    env = {
        "GIT_AUTHOR_NAME": "CodePilot Snapshot",
        "GIT_AUTHOR_EMAIL": "snapshot@example.invalid",
        "GIT_COMMITTER_NAME": "CodePilot Snapshot",
        "GIT_COMMITTER_EMAIL": "snapshot@example.invalid",
        "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
    }
    for args in (
        ["init", "--quiet"],
        ["add", "-f", "--all"],
        [
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=",
            "commit",
            "--quiet",
            "--allow-empty",
            "-m",
            "directory snapshot",
        ],
    ):
        run_git(args, cwd=target, env=env)


@contextmanager
def indexed_repository(task: RetrievalInput, cache_dir: Path) -> Iterator[IndexedRepository]:
    """Owned temporary workspace and in-memory DB; no task labels accepted."""
    source = normalize_source(task.repo)
    if source.local_path is not None and not source.local_path.is_dir():
        raise ValueError(f"Repository directory missing: {source.local_path}")
    snapshot = source.local_path is not None and not (source.local_path / ".git").exists()
    if snapshot and task.base_commit not in {"HEAD", "snapshot"}:
        raise ValueError(
            "Directory snapshot has no Git history; base_commit must be HEAD or snapshot"
        )
    if not snapshot and task.base_commit == "snapshot":
        raise ValueError("Git dataset requires an explicit full base SHA, not snapshot")
    parent = cache_dir / "cli-workspaces"
    parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix="retrieval-", dir=parent))
    engine = create_engine_from_url("sqlite://")
    try:
        repo = task.repo
        if snapshot:
            assert source.local_path is not None
            path = temporary / "source"
            _snapshot(source.local_path, path)
            repo = str(path)
        manager = WorkspaceManager(
            cache_root=temporary / "repos" if snapshot else cache_dir / "repos"
        )
        workspace = manager.create(
            source=repo,
            base_commit="HEAD" if snapshot else task.base_commit,
            target=temporary / "workspace",
        )
        history = GitHistory.for_workspace(manager, workspace).file_churn()
        if snapshot:
            workspace = replace(
                workspace, repository_id=repository_id(source, workspace.base_commit)
            )
        init_db(engine)
        with session_scope(create_session_factory(engine)) as session:
            stores = Stores(session)
            Indexer().index(stores, workspace)
            symbols = tuple(RepositoryIndex(stores, workspace.repository_id).symbol_objects())
            graph = RepoGraph.from_stores(stores, workspace.repository_id)
        yield IndexedRepository(
            workspace.path,
            symbols,
            graph,
            history,
            workspace.base_commit,
            "directory_snapshot" if snapshot else "git",
        )
    finally:
        engine.dispose()
        remove_tree(temporary)
