"""Gold-free inputs accepted by retrieval execution."""

from pydantic import BaseModel, ConfigDict, Field


class RetrievalInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    task_id: str = Field(min_length=1)
    repo: str = Field(min_length=1)
    base_commit: str = Field(min_length=1)
    issue: str = Field(min_length=1)
