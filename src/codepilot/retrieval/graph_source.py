"""The single graph capability the second-stage retrieval channels read.

``RepoGraph`` (R-17) already provides it; channels declare it as a Protocol so
they can be built and scored on an in-memory adjacency in unit tests, without an
index, a database, or a repository on disk.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from codepilot.repo.graph import STRUCTURAL_KINDS, Direction, GraphNeighbor
from codepilot.repo.models import EdgeKind


class GraphSource(Protocol):
    def adjacent(
        self,
        qname: str,
        kinds: Sequence[EdgeKind] = STRUCTURAL_KINDS,
        *,
        direction: Direction = "both",
    ) -> Sequence[GraphNeighbor]: ...
