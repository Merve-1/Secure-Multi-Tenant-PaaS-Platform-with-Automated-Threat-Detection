"""Shared request-model base class and path/query parameter types."""
from typing import Annotated
from uuid import UUID

from fastapi import Path, Query
from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    """Unknown fields are rejected, so a client can never smuggle in values such
    as a tenant ID or a role."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


ProjectId = Annotated[UUID, Path(description="Project ID (UUID)")]
TaskId = Annotated[UUID, Path(description="Task ID (UUID)")]
Limit = Annotated[int, Query(ge=1, le=100)]
Cursor = Annotated[str | None, Query(max_length=200)]