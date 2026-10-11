"""Independent channel switches; defaults preserve the V-01 single BM25 path."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

SeedChannel = Literal["bm25", "ripgrep", "semantic"]
ServiceChannel = Literal["bm25", "ripgrep", "semantic", "structural", "test", "history"]
CHANNELS: tuple[ServiceChannel, ...] = (
    "bm25",
    "ripgrep",
    "semantic",
    "structural",
    "test",
    "history",
)


class RetrievalConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    bm25: bool = True
    ripgrep: bool = False
    semantic: bool = False
    structural: bool = False
    test: bool = False
    history: bool = False

    @property
    def enabled_channels(self) -> tuple[ServiceChannel, ...]:
        return tuple(name for name in CHANNELS if getattr(self, name))
