from __future__ import annotations

from pydantic import BaseModel, Field


class CreateGroupRequest(BaseModel):
    name: str = Field(min_length=1)
    client_api_key: str = Field(min_length=1)
    upstream_api_key: str = Field(min_length=1)
    urls: list[str] = Field(default_factory=list)


class AddUrlsRequest(BaseModel):
    urls: list[str] = Field(min_length=1)


class GroupSummary(BaseModel):
    id: int
    name: str
    alive_count: int
    revival_count: int


class EndpointInfo(BaseModel):
    id: int
    base_url: str
    pool: str
    call_count: int
    last_error: str | None
    last_checked_at: str | None
    added_at: str | None


class GroupDetail(BaseModel):
    id: int
    name: str
    client_api_key: str
    last_used_model: str | None
    endpoints: list[EndpointInfo]
