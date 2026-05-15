from __future__ import annotations

from pydantic import BaseModel, Field


class AddUrlsRequest(BaseModel):
    urls: list[str] = Field(min_length=1)


class UrlSyncRunRequest(BaseModel):
    file: str | None = None


class EndpointInfo(BaseModel):
    id: int
    base_url: str
    pool: str
    call_count: int
    last_error: str | None
    last_checked_at: str | None
    added_at: str | None
