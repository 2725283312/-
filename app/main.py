from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.config import Settings, load_settings
from app.db import get_conn, init_db
from app.pool_manager import PoolManager
from app.schemas import AddUrlsRequest, EndpointInfo, UrlSyncRunRequest
from app.url_utils import normalize_base_url


def get_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    prefix = "Bearer "
    if not authorization.startswith(prefix):
        return None
    token = authorization[len(prefix):].strip()
    return token or None


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    init_db(settings.db_path)
    manager = PoolManager(settings)
    app.state.settings = settings
    app.state.pool_manager = manager

    stop_event = asyncio.Event()

    async def revival_loop() -> None:
        while not stop_event.is_set():
            await manager.revive_once()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=settings.revival_check_interval)
            except asyncio.TimeoutError:
                pass

    tasks = [asyncio.create_task(revival_loop())]

    if settings.url_sync_file:
        async def url_sync_loop() -> None:
            while not stop_event.is_set():
                manager.sync_urls_from_file(settings.url_sync_file)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=settings.url_sync_interval)
                except asyncio.TimeoutError:
                    pass
        tasks.append(asyncio.create_task(url_sync_loop()))

    try:
        yield
    finally:
        stop_event.set()
        await asyncio.gather(*tasks)


app = FastAPI(title="AI API Proxy", version="2.0.0", lifespan=lifespan)
app.mount("/ui", StaticFiles(directory="app/static", html=True), name="ui")


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_pool_manager(request: Request) -> PoolManager:
    return request.app.state.pool_manager


def require_admin(
    settings: Settings = Depends(get_settings),
    authorization: str | None = Header(default=None),
) -> None:
    token = get_bearer_token(authorization)
    if token != settings.admin_token:
        raise HTTPException(status_code=401, detail="invalid admin token")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def root() -> RedirectResponse:
    return RedirectResponse(url="/ui/")


@app.post("/admin/urls", dependencies=[Depends(require_admin)])
def add_urls(payload: AddUrlsRequest, request: Request) -> dict[str, int]:
    manager = get_pool_manager(request)
    inserted = 0
    for url in payload.urls:
        if manager.upsert_endpoint_alive(url):
            inserted += 1
    return {"inserted_urls": inserted}


@app.get("/admin/urls", response_model=list[EndpointInfo], dependencies=[Depends(require_admin)])
def list_urls(request: Request) -> list[EndpointInfo]:
    settings = get_settings(request)
    with get_conn(settings.db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, base_url, pool, call_count, last_error, last_checked_at, added_at
            FROM endpoints
            ORDER BY pool, id
            """
        ).fetchall()
    return [
        EndpointInfo(
            id=row["id"],
            base_url=row["base_url"],
            pool=row["pool"],
            call_count=row["call_count"],
            last_error=row["last_error"],
            last_checked_at=row["last_checked_at"],
            added_at=row["added_at"],
        )
        for row in rows
    ]


@app.post("/admin/url-sync/run", dependencies=[Depends(require_admin)])
def run_url_sync(request: Request, payload: UrlSyncRunRequest | None = None) -> dict[str, Any]:
    settings = get_settings(request)
    manager = get_pool_manager(request)

    file_path = (payload.file.strip() if payload and payload.file and payload.file.strip() else None) or settings.url_sync_file
    if not file_path:
        raise HTTPException(status_code=400, detail="未指定文件路径，请在界面填写或配置 URL_SYNC_FILE 环境变量")

    inserted = manager.sync_urls_from_file(file_path)
    return {"inserted_urls": inserted, "file": file_path}


@app.api_route("/v1/{full_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy_openai(
    full_path: str,
    request: Request,
    manager: PoolManager = Depends(get_pool_manager),
    authorization: str | None = Header(default=None),
):
    settings = get_settings(request)
    token = get_bearer_token(authorization)
    if token != settings.client_api_key:
        return JSONResponse(status_code=401, content={"error": "invalid api key"})

    body = await request.body()
    is_stream = False
    if body:
        try:
            parsed = json.loads(body.decode("utf-8"))
            if isinstance(parsed, dict) and parsed.get("stream") is True:
                is_stream = True
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass

    header_map = dict(request.headers)
    query_map = dict(request.query_params)

    if is_stream:
        status_code, stream, resp_headers = await manager.proxy_with_failover_stream(
            method=request.method,
            path=full_path,
            body=body,
            headers=header_map,
            query_params=query_map,
        )
        return StreamingResponse(stream, status_code=status_code, headers=resp_headers)

    status_code, content, upstream_headers = await manager.proxy_with_failover(
        method=request.method,
        path=full_path,
        body=body,
        headers=header_map,
        query_params=query_map,
    )
    response_headers = {
        k: v for k, v in upstream_headers.items()
        if k.lower() not in {"content-length", "transfer-encoding", "connection"}
    }
    return Response(status_code=status_code, content=content, headers=response_headers)
